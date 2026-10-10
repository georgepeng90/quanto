"""Frozen capability checks use immutable inputs and the caller's SQLite transaction."""
from dataclasses import replace
import shutil

import pytest

from quanto.workbench.configuration import FrameworkResolver, assert_unchanged
from quanto.workbench.frozen_configuration import verify_frozen_configuration
from quanto.workbench.bindings import BindingStore
from quanto.workbench.framework_requests import ResumeRequest
from quanto.workbench.framework_requests import FixedRequest, PolicyBacktestRequest
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import policy_identity
from quanto.learning.artifacts import write_manifest
from quanto.objects import digest, json_bytes
from quanto.workbench.store import utc_now
from tests.integration.test_framework_capabilities import _training_case
from tests.integration.test_model_framework_bindings import _old_model
from tests.integration.test_snapshot_submission import _ready


def test_resolver_reuses_caller_transaction(tmp_path):
    store, request, _ = _training_case(tmp_path)
    resolver = FrameworkResolver(store)
    before = resolver.resolve(request)
    with store.transaction() as db:
        after = resolver.resolve(request, db=db)
        assert resolver.check(request, db=db).status == "ready"
    assert_unchanged(before, after)


def test_frozen_train_recomputes_capability_from_dataset_and_definitions(tmp_path):
    store, request, _ = _training_case(tmp_path)
    resolved = FrameworkResolver(store).resolve(request)
    dataset = store.get_dataset(request.dataset_id)
    from quanto.learning.registry import hash_files
    ref = {"id": dataset.id, "path": str(dataset.path), "content_hash": dataset.content_hash,
           "files": hash_files(dataset.path)}
    verified = verify_frozen_configuration(resolved, dataset_ref=ref)
    assert_unchanged(resolved, verified)
    changed = replace(dataset, content_hash="f" * 64)
    with pytest.raises(ValueError, match="dataset|hash|frozen"):
        verify_frozen_configuration(resolved, dataset_ref={**ref, "content_hash": changed.content_hash})


def test_frozen_fixed_rechecks_strategy_source_and_declaration(tmp_path):
    store, draft, dataset, strategy = _ready(tmp_path)
    request = FixedRequest.model_validate(draft.payload)
    resolved = FrameworkResolver(store).resolve(request)
    frozen_strategy = tmp_path / "snapshot" / "strategy"
    shutil.copytree(strategy.path, frozen_strategy)
    from quanto.learning.registry import hash_files
    dataset_ref = {"id": dataset.id, "path": str(dataset.path), "content_hash": dataset.content_hash,
                   "files": hash_files(dataset.path)}
    strategy_ref = {"id": strategy.id, "path": str(frozen_strategy), "content_hash": strategy.content_hash,
                    "files": hash_files(frozen_strategy), "manifest": strategy.manifest}
    assert_unchanged(resolved, verify_frozen_configuration(
        resolved, dataset_ref=dataset_ref, strategy_ref=strategy_ref))
    (frozen_strategy / "strategy.py").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="strategy file"):
        verify_frozen_configuration(resolved, dataset_ref=dataset_ref, strategy_ref=strategy_ref)


def test_semantic_comparison_ignores_generation_time_only(tmp_path):
    store, request, _ = _training_case(tmp_path)
    resolved = FrameworkResolver(store).resolve(request)
    later = resolved.model_copy(update={"capability_report": resolved.capability_report.model_copy(
        update={"generated_at": None})})
    assert_unchanged(resolved, later)
    altered = later.model_copy(update={"request_hash": "f" * 64})
    with pytest.raises(ValueError, match="frozen"):
        assert_unchanged(resolved, altered)


def test_frozen_first_resume_rechecks_training_without_live_database(tmp_path):
    store, create, _ = _training_case(tmp_path)
    _old_model(store, create)
    binding = BindingStore(store).bind_model("old-model", "audit")
    request = ResumeRequest(model_id="old-model", additional_timesteps=16)
    resolved = FrameworkResolver(store).resolve(request)
    with store.transaction() as db:
        assert_unchanged(resolved, FrameworkResolver(store).resolve(request, db=db))
    dataset = store.get_dataset(create.dataset_id)
    from quanto.learning.registry import hash_files
    ref = {"id": dataset.id, "path": str(dataset.path), "content_hash": dataset.content_hash,
           "files": hash_files(dataset.path)}
    verified = verify_frozen_configuration(resolved, dataset_ref=ref, model_binding=binding)
    assert_unchanged(resolved, verified)


def test_frozen_policy_rechecks_successful_version_and_purpose(tmp_path):
    store, create, _ = _training_case(tmp_path)
    _old_model(store, create)
    training = FrameworkResolver(store).resolve(create)
    spec = PolicyTrainingSpec.model_validate(training.internal_training_spec)
    dataset = store.get_dataset(create.dataset_id)
    from quanto.learning.registry import hash_files
    artifact = tmp_path / "policy"
    artifact.mkdir()
    (artifact / "policy.zip").write_bytes(b"weights")
    (artifact / "last.zip").write_bytes(b"state")
    manifest = write_manifest(artifact, {"kind": "policy", "version": 1,
        "config": spec.model_dump(mode="json"), "model_id": "old-model",
        "dataset_content_hash": dataset.content_hash, "basket": list(spec.backtest.symbols),
        "price_mode": "raw_cash", "parent_content_id": None, "best_source_content_id": None,
        "cutoff": spec.validation_end.isoformat(), "selection_metric": spec.selection_metric,
        **policy_identity(spec)})
    files = hash_files(artifact)
    with store.transaction() as db:
        db.execute("""INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,created_at)
                      VALUES (?,'train_policy','succeeded',1,?,?)""", ("version-one", str(tmp_path), utc_now()))
        db.execute("INSERT INTO investing_sessions VALUES (?,?,?)", ("version-one", "old-model", None))
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("version-one", "policy", str(artifact), digest(json_bytes(files)),
                    json_bytes(manifest).decode(), json_bytes(files).decode(), utc_now()))
    binding = BindingStore(store).bind_model("old-model", "audit")
    request = PolicyBacktestRequest(version_id="version-one", dataset_id=dataset.id,
                                    spec={"start": spec.validation_end, "end": spec.backtest.end,
                                          "evaluation_purpose": "development_replay"})
    resolved = FrameworkResolver(store).resolve(request)
    with store.transaction() as db:
        assert_unchanged(resolved, FrameworkResolver(store).resolve(request, db=db))
    dataset_ref = {"id": dataset.id, "path": str(dataset.path), "content_hash": dataset.content_hash,
                   "files": hash_files(dataset.path)}
    policy_ref = {"id": "version-one", "path": str(artifact), "content_hash": digest(json_bytes(files)),
                  "files": files}
    verified = verify_frozen_configuration(resolved, dataset_ref=dataset_ref,
                                           model_binding=binding, policy_ref=policy_ref,
                                           training_dataset_ref=dataset_ref)
    assert_unchanged(resolved, verified)
    resume = FrameworkResolver(store).resolve(ResumeRequest(model_id="old-model", additional_timesteps=16))
    assert_unchanged(resume, verify_frozen_configuration(
        resume, dataset_ref=dataset_ref, model_binding=binding, policy_ref=policy_ref))
    with pytest.raises(ValueError, match="policy version reference"):
        verify_frozen_configuration(resolved, dataset_ref=dataset_ref,
                                    model_binding=binding, policy_ref={**policy_ref, "id": "other"},
                                    training_dataset_ref=dataset_ref)
