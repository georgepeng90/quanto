"""Audited model bindings preserve their original source and ordered identity."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256

import pandas as pd
import pytest

from quanto.backtest.dataset_identity import make_import_evidence, standard_table_semantics
from quanto.backtest.dataset_loaders import load_dataset
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetMapping, BenchmarkBinding, canonical_hash, thaw_json
from quanto.investing.evaluation import policy_identity
from quanto.investing.definitions import ETF_MARKET_FEATURES, etf_learning_definitions
from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.investing.legacy_evidence import (FROZEN_ETF_BASELINE_SHA256, FROZEN_ETF_SOURCE_COMMIT,
                                              audited_etf_semantics)
from quanto.learning.artifacts import write_manifest
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.store import WorkbenchStore, utc_now
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.fixtures.investing_training_cases import investing_case
from tests.integration.test_framework_capabilities import _training_case


def _old_model(store, request, *, model_id="old-model"):
    binding = BindingStore(store).get_asset_binding(request.dataset_id)
    pairs = binding.resolve(tuple(asset.instrument_id for asset in binding.assets))
    spec = request.spec.to_spec(pairs).model_dump(mode="json")
    with store.transaction() as db:
        db.execute("INSERT INTO investing_models VALUES (?,?,?,?)",
                   (model_id, request.dataset_id, json.dumps(spec, ensure_ascii=False), utc_now()))
    return spec


def test_old_model_binding_is_ordered_immutable_and_idempotent(tmp_path):
    store, request, _ = _training_case(tmp_path)
    spec = _old_model(store, request)
    bindings = BindingStore(store)
    checked = bindings.inspect_model_binding("old-model")
    assert checked.status == "ready", checked.model_dump(mode="json")
    linked = bindings.bind_model("old-model", "first")
    assert linked.original_spec_hash == canonical_hash(spec)
    assert linked.basket.members == tuple(pair.instrument_id for pair in linked.ordered_pairs)
    assert linked.training_configuration.operation == "train"
    assert linked.origin == "legacy_audited"
    assert bindings.bind_model("old-model", "first") == linked
    assert bindings.bind_model("old-model", "second") == linked
    assert bindings.get_model_binding("old-model") == linked
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM baskets").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM model_framework_bindings").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM framework_idempotency WHERE scope='model.bind'").fetchone()[0] == 2


def test_old_model_binding_rejects_unproven_local_identity(tmp_path):
    store, request, _ = _training_case(tmp_path)
    spec = _old_model(store, request)
    spec["backtest"]["symbols"] = ["missing-local-key"]
    with store.transaction() as db:
        db.execute("UPDATE investing_models SET spec_json=? WHERE id='old-model'", (json.dumps(spec),))
    bindings = BindingStore(store)
    report = bindings.inspect_model_binding("old-model")
    assert report.status == "blocked" and "IDENTITY_UNRESOLVED" in report.error_codes
    with pytest.raises(ValueError, match="IDENTITY_UNRESOLVED"):
        bindings.bind_model("old-model", "attempt")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM model_framework_bindings").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM baskets").fetchone()[0] == 1


def test_old_model_preserves_original_local_order_in_canonical_basket(tmp_path):
    source = tmp_path / "two-asset-source"
    spec, dates = investing_case(source)
    for name in ("prices", "status", "instruments"):
        path = source / f"{name}.csv"
        frame = pd.read_csv(path, dtype={"symbol": str})
        pd.concat([frame, frame.assign(symbol="000002")], ignore_index=True).to_csv(path, index=False)
    spec["backtest"].update(start=str(dates[65].date()), end=str(dates[99].date()),
                            symbols=["SZSE:etf:000002", "SZSE:etf:000001"])
    spec.update(train_start=str(dates[65].date()), train_end=str(dates[79].date()),
                validation_start=str(dates[80].date()), validation_end=str(dates[89].date()),
                test_start=str(dates[90].date()), test_end=str(dates[99].date()),
                episode_sessions=10)
    store = WorkbenchStore(tmp_path / "workspace")
    dataset = store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    with store.transaction() as db:
        db.execute("INSERT INTO investing_models VALUES (?,?,?,?)",
                   ("reverse-order", dataset.id, json.dumps(spec), utc_now()))
    linked = BindingStore(store).bind_model("reverse-order", "reverse")
    assert [pair.local_symbol for pair in linked.ordered_pairs] == ["SZSE:etf:000002", "SZSE:etf:000001"]
    assert [member.key for member in linked.basket.members] == ["SZSE:etf:000002", "SZSE:etf:000001"]


def test_old_etf_model_can_select_from_dataset_with_extra_stock(tmp_path):
    source = tmp_path / "mixed-source"
    spec, dates = investing_case(source)
    for name in ("prices", "status", "instruments"):
        path = source / f"{name}.csv"
        frame = pd.read_csv(path, dtype={"symbol": str})
        pd.concat([frame, frame.assign(symbol="000002")], ignore_index=True).to_csv(path, index=False)
    bundle = load_dataset(source)
    ref = "fixture:explicit-mixed-identities"
    evidence = make_import_evidence(source, assets=(
        AssetMapping(instrument_id=InstrumentId(exchange="SZSE", instrument_type="etf", code="000001"),
                     local_symbol="000001", evidence_ref=ref),
        AssetMapping(instrument_id=InstrumentId(exchange="SZSE", instrument_type="stock", code="000002"),
                     local_symbol="000002", evidence_ref=ref)),
        benchmark=BenchmarkBinding(custom_series_id="synthetic-benchmark",
                                   return_basis=bundle.manifest.benchmark_type, evidence_ref=ref),
        source_document="Explicit synthetic ETF and stock identity evidence.",
        table_semantics=standard_table_semantics(bundle, ref))
    spec["backtest"].update(start=str(dates[65].date()), end=str(dates[99].date()),
                            symbols=["SZSE:etf:000001"])
    spec.update(train_start=str(dates[65].date()), train_end=str(dates[79].date()),
                validation_start=str(dates[80].date()), validation_end=str(dates[89].date()),
                test_start=str(dates[90].date()), test_end=str(dates[99].date()), episode_sessions=10)
    store = WorkbenchStore(tmp_path / "workspace")
    dataset = store.register_dataset(source, identity_evidence=evidence)
    with store.transaction() as db:
        db.execute("INSERT INTO investing_models VALUES (?,?,?,?)",
                   ("selected-etf", dataset.id, json.dumps(spec), utc_now()))
    linked = BindingStore(store).bind_model("selected-etf", "selected")
    assert [pair.instrument_id.instrument_type for pair in linked.ordered_pairs] == ["etf"]
    assert {pair.instrument_id.instrument_type for pair in linked.asset_binding.assets} == {"etf", "stock"}


def test_inspecting_bound_model_does_not_reaudit_later_framework_versions(tmp_path):
    store, request, _ = _training_case(tmp_path)
    _old_model(store, request)
    bindings = BindingStore(store)
    linked = bindings.bind_model("old-model", "first")
    with store.transaction() as db:
        db.execute("""INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,created_at)
                      VALUES (?,'train_policy','succeeded',1,?,?)""", ("later-version", str(tmp_path), utc_now()))
        db.execute("INSERT INTO investing_sessions VALUES (?,?,?)", ("later-version", "old-model", None))
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("later-version", "policy", str(tmp_path), "a" * 64,
                    json.dumps({"framework_learning": {"new": True}}), "{}", utc_now()))
    assert bindings.inspect_model_binding("old-model").status == "ready"
    assert bindings.get_model_binding("old-model") == linked


def test_packaged_etf_proof_matches_frozen_baseline_and_validated_definitions(monkeypatch):
    from pathlib import Path

    reference = Path(__file__).resolve().parents[1] / "fixtures" / "etf_framework_baseline.json"
    raw = reference.read_bytes()
    assert FROZEN_ETF_BASELINE_SHA256 == sha256(raw).hexdigest()
    assert FROZEN_ETF_SOURCE_COMMIT == json.loads(raw)["evidence"]["source_commit"]

    def no_test_file_reads(path):
        if "tests" in Path(path).parts:
            raise AssertionError("Model binding runtime must not read test files")
        return raw_read(path)

    raw_read = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", no_test_file_reads)
    for definition in etf_learning_definitions():
        proof = audited_etf_semantics(definition, ETF_TRADING_DEFINITION)
        assert proof["validated_definition_hashes"] == {
            "learning": definition.definition_hash,
            "feature": ETF_MARKET_FEATURES.definition_hash,
            "trading": ETF_TRADING_DEFINITION.definition_hash}
        assert proof["baseline_sha256"] == FROZEN_ETF_BASELINE_SHA256
        assert proof["source_commit"] == FROZEN_ETF_SOURCE_COMMIT


def test_packaged_etf_proof_rejects_definition_drift():
    definition = etf_learning_definitions()[0]
    changed = replace(definition, implementation_hash="0" * 64)
    with pytest.raises(ValueError, match="DEFINITION_UNAVAILABLE"):
        audited_etf_semantics(changed, ETF_TRADING_DEFINITION)
    with pytest.raises(ValueError, match="DEFINITION_UNAVAILABLE"):
        audited_etf_semantics(definition, replace(ETF_TRADING_DEFINITION, implementation_hash="0" * 64))
    changed_feature = replace(ETF_MARKET_FEATURES, implementation_hash="0" * 64)
    with pytest.raises(ValueError, match="DEFINITION_UNAVAILABLE"):
        audited_etf_semantics(replace(definition, feature_definition=changed_feature), ETF_TRADING_DEFINITION)


def test_legacy_binding_needs_no_test_fixture_at_runtime(tmp_path, monkeypatch):
    from pathlib import Path

    store, request, _ = _training_case(tmp_path)
    _old_model(store, request)
    raw_read, raw_is_file = Path.read_bytes, Path.is_file

    def read_without_tests(path):
        if "tests" in Path(path).parts:
            raise AssertionError("Runtime attempted to read tests")
        return raw_read(path)

    def is_file_without_tests(path):
        if "tests" in Path(path).parts:
            raise AssertionError("Runtime attempted to inspect tests")
        return raw_is_file(path)

    monkeypatch.setattr(Path, "read_bytes", read_without_tests)
    monkeypatch.setattr(Path, "is_file", is_file_without_tests)
    linked = BindingStore(store).bind_model("old-model", "packaged-proof")
    assert linked.evidence["equivalence_reference"]["baseline_sha256"] == FROZEN_ETF_BASELINE_SHA256


@pytest.mark.parametrize("keys", [("same", "same"), ("first", "second")])
def test_concurrent_legacy_binding_publishes_one_model_basket(tmp_path, keys):
    store, request, _ = _training_case(tmp_path)
    _old_model(store, request)
    bindings = BindingStore(store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda key: bindings.bind_model("old-model", key), keys))
    assert first == second
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM baskets WHERE basket_id='model-old-model'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM basket_revisions WHERE basket_id='model-old-model'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM model_framework_bindings WHERE model_id='old-model'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM framework_idempotency WHERE scope='model.bind'").fetchone()[0] == len(set(keys))


def test_legacy_binding_rolls_back_basket_and_idempotency_after_insert_failure(tmp_path, monkeypatch):
    store, request, _ = _training_case(tmp_path)
    original = _old_model(store, request)
    bindings = BindingStore(store)
    with store.transaction() as db:
        before = db.execute("SELECT spec_json FROM investing_models WHERE id='old-model'").fetchone()[0]

    def fail_after_basket(db, binding):
        assert db.execute("SELECT 1 FROM basket_revisions WHERE basket_id='model-old-model'").fetchone()
        raise RuntimeError("injected binding insert failure")

    monkeypatch.setattr(bindings, "_insert_model_binding", fail_after_basket)
    with pytest.raises(RuntimeError, match="injected binding insert failure"):
        bindings.bind_model("old-model", "failed-at-insert")
    with store.transaction() as db:
        assert db.execute("SELECT spec_json FROM investing_models WHERE id='old-model'").fetchone()[0] == before
        assert json.loads(before) == original
        assert db.execute("SELECT count(*) FROM baskets WHERE basket_id='model-old-model'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM basket_revisions WHERE basket_id='model-old-model'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM model_framework_bindings").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM framework_idempotency WHERE scope='model.bind'").fetchone()[0] == 0


def test_model_binding_insert_and_model_row_roll_back_together(tmp_path):
    store, request, _ = _training_case(tmp_path)
    _old_model(store, request)
    linked = BindingStore(store).bind_model("old-model", "first")
    with pytest.raises(RuntimeError, match="rollback"):
        with store.transaction() as db:
            db.execute("INSERT INTO investing_models VALUES (?,?,?,?)",
                       ("new-model", request.dataset_id, json.dumps(thaw_json(linked.original_spec)), utc_now()))
            candidate = linked.model_copy(update={"model_id": "new-model", "binding_hash": ""})
            BindingStore(store)._insert_model_binding(db, candidate)
            raise RuntimeError("rollback")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM investing_models WHERE id='new-model'").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM model_framework_bindings WHERE model_id='new-model'").fetchone()[0] == 0


def test_old_policy_files_and_manifest_are_audited_before_binding(tmp_path):
    store, request, _ = _training_case(tmp_path)
    _old_model(store, request)
    spec = request.spec.to_spec(BindingStore(store).get_asset_binding(request.dataset_id).resolve(
        tuple(asset.instrument_id for asset in BindingStore(store).get_asset_binding(request.dataset_id).assets)))
    artifact = tmp_path / "policy"
    artifact.mkdir()
    (artifact / "last.zip").write_bytes(b"optimizer-state")
    (artifact / "policy.zip").write_bytes(b"selected-weights")
    manifest = write_manifest(artifact, {"kind": "policy", "version": 1,
        "config": spec.model_dump(mode="json"), "model_id": "old-model",
        "dataset_content_hash": BindingStore(store).get_asset_binding(request.dataset_id).dataset_content_hash,
        "basket": list(spec.backtest.symbols), "price_mode": "raw_cash",
        "parent_content_id": None, "best_source_content_id": None,
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
        original_row = db.execute("SELECT spec_json FROM investing_models WHERE id='old-model'").fetchone()[0]
    original_artifact = {path.name: path.read_bytes() for path in artifact.iterdir()}
    bindings = BindingStore(store)
    assert bindings.inspect_model_binding("old-model").status == "ready"
    with store.transaction() as db:
        assert db.execute("SELECT spec_json FROM investing_models WHERE id='old-model'").fetchone()[0] == original_row
    assert {path.name: path.read_bytes() for path in artifact.iterdir()} == original_artifact
    candidate = bindings._prepare_model_binding("old-model")
    assert candidate.evidence["policy_artifacts"]["version-one"]["manifest_id"] == manifest["id"]
    assert "Original source snapshot unavailable" in candidate.evidence["limitations"][0]
    (artifact / "policy.zip").write_bytes(b"altered")
    report = bindings.inspect_model_binding("old-model")
    assert report.status == "blocked" and "BINDING_MISMATCH" in report.error_codes
    with pytest.raises(ValueError, match="ARTIFACT|BINDING_MISMATCH"):
        bindings.bind_model("old-model", "tampered")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM model_framework_bindings").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM baskets").fetchone()[0] == 1
