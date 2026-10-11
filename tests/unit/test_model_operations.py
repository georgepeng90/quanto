"""Model operations preserve the training action space and research identity."""
from datetime import datetime, timezone

import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.framework import AssetBinding, ModelFrameworkBinding
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import policy_identity
from quanto.investing.framework import model_request_from_spec, validate_model_operation
from quanto.learning.artifacts import write_manifest
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.etf_model_migration import ModelMigrator
from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.framework_requests import PolicyBacktestRequest, ResumeRequest
from quanto.workbench.store import utc_now
from tests.integration.test_framework_capabilities import _training_case
from tests.integration.test_model_framework_bindings import _old_model


@pytest.fixture
def frozen_model(tmp_path):
    store, request, _ = _training_case(tmp_path)
    resolved = FrameworkResolver(store).resolve(request)
    spec = PolicyTrainingSpec.model_validate(resolved.internal_training_spec)
    binding = ModelFrameworkBinding(model_id="model",
                                    original_spec=spec.model_dump(mode="json"),
                                    training_configuration=resolved,
                                    semantic_identity=policy_identity(spec),
                                    evidence={"method": "resolved-new-model"},
                                    created_at=datetime.now(timezone.utc))
    return binding, spec


def _operation(binding, operation, *, training=None, pairs=None, asset_binding=None, backtest=None):
    frozen = binding.training_configuration
    report = frozen.capability_report.model_copy(update={"operation": operation})
    return frozen.model_copy(update={"operation": operation, "capability_report": report,
                                     "internal_training_spec": training,
                                     "internal_backtest_spec": backtest or frozen.internal_backtest_spec,
                                     "ordered_pairs": pairs or frozen.ordered_pairs,
                                     "asset_binding": asset_binding or frozen.asset_binding})


def test_model_request_reverses_local_symbols_and_risk_keys(frozen_model):
    binding, spec = frozen_model
    local = binding.ordered_pairs[0].local_symbol
    values = spec.model_dump(mode="json")
    values["backtest"]["risk"]["stress_scenarios"] = [{"name": "shock", "symbol_shocks": {local: -.1},
                                                            "execution_cost_rate": 0.}]
    spec = PolicyTrainingSpec.model_validate(values)
    learning = next(item.ref for item in binding.training_configuration.definitions if item.ref.kind == "learning")
    trading = next(item.ref for item in binding.training_configuration.definitions if item.ref.kind == "trading")
    request = model_request_from_spec(spec, binding.basket, binding.asset_binding, learning, trading)
    assert request.spec.backtest.to_spec(binding.ordered_pairs) == spec.backtest
    assert "symbols" not in request.model_dump(mode="json")["spec"]["backtest"]


def test_resume_changes_only_steps_and_device(frozen_model):
    binding, spec = frozen_model
    request = ResumeRequest(model_id="model", additional_timesteps=32, device="cpu")
    values = spec.model_dump(mode="json")
    values["total_timesteps"] = 32
    validate_model_operation(binding, request, _operation(binding, "resume", training=values))
    values["train_start"] = "2024-01-02"
    with pytest.raises(ValueError, match="MODEL_IDENTITY_MISMATCH"):
        validate_model_operation(binding, request, _operation(binding, "resume", training=values))


def test_new_coverage_requires_exact_canonical_and_local_order(frozen_model):
    binding, spec = frozen_model
    start = spec.validation_end
    request = PolicyBacktestRequest(version_id="version", dataset_id="coverage",
                                    spec={"start": start, "end": spec.backtest.end,
                                          "evaluation_purpose": "development_replay"})
    resolved = _operation(binding, "policy_backtest")
    validate_model_operation(binding, request, resolved)
    first = binding.ordered_pairs[0]
    changed_local = first.model_copy(update={"local_symbol": "DIFFERENT"})
    with pytest.raises(ValueError, match="ordered canonical/local"):
        validate_model_operation(binding, request,
                                 _operation(binding, "policy_backtest", pairs=(changed_local, *binding.ordered_pairs[1:])))
    changed_canonical = first.model_copy(update={"instrument_id": first.instrument_id.model_copy(update={"code": "999999"})})
    with pytest.raises(ValueError, match="ordered canonical/local"):
        validate_model_operation(binding, request,
                                 _operation(binding, "policy_backtest", pairs=(changed_canonical, *binding.ordered_pairs[1:])))


def test_backtest_purpose_and_cost_scaling_are_explicit(frozen_model):
    binding, spec = frozen_model
    request = PolicyBacktestRequest(version_id="version", dataset_id="coverage",
                                    spec={"start": spec.validation_end, "end": spec.backtest.end,
                                          "evaluation_purpose": "post_selection_backtest", "cost_multiplier": 2})
    with pytest.raises(ValueError, match="development_replay"):
        validate_model_operation(binding, request, _operation(binding, "policy_backtest"))
    replay = request.model_copy(update={"spec": request.spec.model_copy(update={"evaluation_purpose": "development_replay"})})
    values = spec.backtest.model_dump(mode="json")
    trading = next(item for item in binding.training_configuration.definitions if item.ref.kind == "trading")
    for name in trading.declaration["scalable_costs"]:
        values["execution"][name] *= 2
    validate_model_operation(binding, replay, _operation(binding, "policy_backtest", backtest=values))
    values["execution"]["max_stale_sessions"] += 1
    with pytest.raises(ValueError, match="execution condition"):
        validate_model_operation(binding, replay, _operation(binding, "policy_backtest", backtest=values))


def test_first_model_session_resolves_without_parent_version(tmp_path, monkeypatch):
    store, request, _ = _training_case(tmp_path)
    resolved = FrameworkResolver(store).resolve(request)
    spec = PolicyTrainingSpec.model_validate(resolved.internal_training_spec)
    binding = ModelFrameworkBinding(model_id="model",
                                    original_spec=spec.model_dump(mode="json"),
                                    training_configuration=resolved,
                                    semantic_identity=policy_identity(spec),
                                    evidence={"method": "resolved-new-model"},
                                    created_at=datetime.now(timezone.utc))
    with store.transaction() as db:
        db.execute("INSERT INTO investing_models VALUES (?,?,?,?)",
                   ("model", request.dataset_id, json_bytes(spec.model_dump(mode="json")).decode(),
                    datetime.now(timezone.utc).isoformat()))
    monkeypatch.setattr(BindingStore, "get_model_binding", lambda self, model_id: binding, raising=False)
    report = FrameworkResolver(store).check(ResumeRequest(model_id="model", additional_timesteps=16))
    assert report.status == "ready", report.checks


def test_model_row_keeps_original_spec_hash_when_default_was_omitted(tmp_path, monkeypatch):
    store, request, _ = _training_case(tmp_path)
    resolver = FrameworkResolver(store)
    resolved = resolver.resolve(request)
    spec = PolicyTrainingSpec.model_validate(resolved.internal_training_spec)
    original = spec.model_dump(mode="json")
    del original["selection_metric"]
    binding = ModelFrameworkBinding(model_id="model",
                                    original_spec=original, training_configuration=resolved,
                                    semantic_identity=policy_identity(spec),
                                    evidence={"method": "audited-original-spec"},
                                    created_at=datetime.now(timezone.utc))
    with store.transaction() as db:
        db.execute("INSERT INTO investing_models VALUES (?,?,?,?)",
                   ("model", request.dataset_id, json_bytes(original).decode(),
                    datetime.now(timezone.utc).isoformat()))
    monkeypatch.setattr(BindingStore, "get_model_binding", lambda self, model_id: binding, raising=False)
    checked, normalized, _ = resolver._model_binding("model")
    assert checked.original_spec_hash == binding.original_spec_hash
    assert normalized.selection_metric == spec.selection_metric


def test_audited_model_first_session_uses_registered_binding(tmp_path):
    store, create, _ = _training_case(tmp_path)
    _old_model(store, create)
    ModelMigrator(store).migrate_model("old-model")
    resolved = FrameworkResolver(store).resolve(ResumeRequest(model_id="old-model", additional_timesteps=16))
    assert resolved.operation == "resume"
    assert resolved.internal_training_spec["total_timesteps"] == 16
    assert resolved.capability_report.status == "ready"


def test_policy_version_rejects_registry_hash_that_disagrees_with_file_list(tmp_path):
    store, create, _ = _training_case(tmp_path)
    _old_model(store, create)
    resolver = FrameworkResolver(store)
    training = resolver.resolve(create)
    spec = PolicyTrainingSpec.model_validate(training.internal_training_spec)
    artifact = tmp_path / "policy"
    artifact.mkdir()
    (artifact / "policy.zip").write_bytes(b"selected-weights")
    (artifact / "last.zip").write_bytes(b"optimizer-state")
    manifest = write_manifest(artifact, {"kind": "policy", "version": 1,
        "config": spec.model_dump(mode="json"), "model_id": "old-model",
        "dataset_content_hash": training.asset_binding.dataset_content_hash,
        "basket": list(spec.backtest.symbols),
        "price_mode": load_dataset(store.get_dataset(create.dataset_id).path).price_mode,
        "parent_content_id": None, "best_source_content_id": None,
        "cutoff": spec.validation_end.isoformat(), "selection_metric": spec.selection_metric,
        **policy_identity(spec)})
    files = hash_files(artifact)
    with store.transaction() as db:
        db.execute("""INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,created_at)
                      VALUES (?,'train_policy','succeeded',1,?,?)""",
                   ("version-one", str(tmp_path), utc_now()))
        db.execute("INSERT INTO investing_sessions VALUES (?,?,?)", ("version-one", "old-model", None))
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("version-one", "policy", str(artifact), digest(json_bytes(files)),
                    json_bytes(manifest).decode(), json_bytes(files).decode(), utc_now()))
    binding = ModelMigrator(store).migrate_model("old-model")
    with store.transaction() as db:
        db.execute("UPDATE learning_artifacts SET content_hash=? WHERE id=?", ("0" * 64, "version-one"))
    with pytest.raises(ValueError, match="registry hash"):
        resolver._verified_policy_version(binding, "version-one", spec,
            {item.ref.kind: resolver.registry.get(item.ref) for item in training.definitions})


def test_policy_backtest_checks_new_dataset_without_requiring_train_dates(tmp_path, monkeypatch):
    store, create, _ = _training_case(tmp_path)
    resolver = FrameworkResolver(store)
    training = resolver.resolve(create)
    spec = PolicyTrainingSpec.model_validate(training.internal_training_spec)
    binding = ModelFrameworkBinding(model_id="model",
                                    original_spec=spec.model_dump(mode="json"),
                                    training_configuration=training,
                                    semantic_identity=policy_identity(spec),
                                    evidence={"method": "resolved-new-model"},
                                    created_at=datetime.now(timezone.utc))
    monkeypatch.setattr(resolver, "_verified_policy_version",
                        lambda *args: ({"id": "a" * 64,
                                        "price_mode": load_dataset(store.get_dataset(create.dataset_id).path).price_mode},
                                       {"content_hash": "b" * 64}))
    request = PolicyBacktestRequest(version_id="version", dataset_id=create.dataset_id,
                                    spec={"start": spec.validation_end, "end": spec.backtest.end,
                                          "evaluation_purpose": "development_replay"})
    resolved, report = resolver._resolve_policy_backtest(
        request, binding, spec, {kind: resolver.registry.get(item.ref)
                                 for item in training.definitions for kind in (item.ref.kind,)})
    assert report.status == "ready", report.checks
    assert tuple(resolved.internal_backtest_spec["symbols"]) == tuple(spec.backtest.symbols)


@pytest.mark.parametrize("change", ["unit", "availability", "benchmark_basis"])
def test_policy_backtest_rejects_changed_training_semantics(tmp_path, monkeypatch, change):
    store, create, _ = _training_case(tmp_path)
    resolver = FrameworkResolver(store)
    training = resolver.resolve(create)
    spec = PolicyTrainingSpec.model_validate(training.internal_training_spec)
    binding = ModelFrameworkBinding(model_id="model",
                                    original_spec=spec.model_dump(mode="json"),
                                    training_configuration=training,
                                    semantic_identity=policy_identity(spec),
                                    evidence={"method": "resolved-new-model"},
                                    created_at=datetime.now(timezone.utc))
    raw = training.asset_binding.model_dump(mode="json")
    raw["binding_hash"] = ""
    if change == "benchmark_basis":
        raw["benchmark"]["return_basis"] = "total_return"
    else:
        used = training.requirements[0]
        declaration = next(item for item in raw["table_semantics"] if item["table"] == used.table)
        declaration["declaration_hash"] = ""
        if change == "unit":
            declaration["schema_definition"][used.fields[0].name]["unit"] = "changed-unit"
        else:
            declaration["temporal_rules"]["availability_rule"] = "changed-rule"
    altered = AssetBinding.model_validate(raw)
    monkeypatch.setattr(BindingStore, "get_asset_binding", lambda self, dataset_id: altered)
    monkeypatch.setattr(resolver, "_verified_policy_version",
                        lambda *args: ({"id": "a" * 64,
                                        "price_mode": load_dataset(store.get_dataset(create.dataset_id).path).price_mode},
                                       {"content_hash": "b" * 64}))
    request = PolicyBacktestRequest(version_id="version", dataset_id=create.dataset_id,
                                    spec={"start": spec.validation_end, "end": spec.backtest.end,
                                          "evaluation_purpose": "development_replay"})
    _, report = resolver._resolve_policy_backtest(
        request, binding, spec, {item.ref.kind: resolver.registry.get(item.ref) for item in training.definitions})
    assert report.status == "blocked"
    assert "MODEL_IDENTITY_MISMATCH" in report.error_codes
