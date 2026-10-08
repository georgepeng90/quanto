from dataclasses import replace
import json

import pandas as pd
import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.dataset_storage import write_parquet_dataset
from quanto.data.contracts import InstrumentId
from quanto.data.dataset_identity import make_import_evidence, standard_table_semantics
from quanto.framework import AssetMapping, BenchmarkBinding
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.revisions import source_files
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.backtest_cases import dataset_case


def evidence_for(root):
    bundle = load_dataset(root)
    return make_import_evidence(root,
        assets=(AssetMapping(instrument_id=InstrumentId(exchange="SZSE", instrument_type="etf", code="000001"),
                             local_symbol="000001", evidence_ref="fixture:identities"),),
        benchmark=BenchmarkBinding(custom_series_id="synthetic-benchmark", return_basis="price_index", evidence_ref="fixture:identities"),
        source_document="Explicit synthetic identity declaration; no real security inference.",
        table_semantics=standard_table_semantics(bundle, "fixture:identities"))


def test_new_registration_publishes_canonical_symbols_and_binding_together(tmp_path):
    source = dataset_case(tmp_path / "source")
    before = source_files(source)
    store = WorkbenchStore(tmp_path / "workspace")
    version = store.register_dataset(source, identity_evidence=evidence_for(source))
    bundle = load_dataset(version.path)
    binding = BindingStore(store).get_asset_binding(version.id)
    assert set(bundle.prices.symbol) == {"SZSE:etf:000001"}
    assert binding.assets[0].instrument_id.key == binding.assets[0].local_symbol == "SZSE:etf:000001"
    assert binding.dataset_content_hash == version.content_hash
    assert binding.benchmark.custom_series_id == "synthetic-benchmark"
    assert {item.table for item in binding.table_semantics} == {"prices", "benchmark", "calendar", "instruments", "status", "events"}
    assert (version.path / "identity_evidence.json").is_file()
    assert source_files(source) == before
    assert BindingStore(store).bind_dataset(version.id, binding.assets[0].evidence_ref, "bind-again") == binding


@pytest.mark.parametrize("damage", ["mapping", "hash", "benchmark", "semantics"])
def test_import_requires_complete_auditable_evidence(tmp_path, damage):
    source = dataset_case(tmp_path / "source")
    evidence = evidence_for(source)
    if damage == "mapping":
        evidence["assets"][0]["local_symbol"] = "another"
    elif damage == "hash":
        evidence["input_files"]["prices.csv"] = "0" * 64
    elif damage == "benchmark":
        del evidence["benchmark"]
    else:
        evidence["table_semantics"] = evidence["table_semantics"][:1]
    store = WorkbenchStore(tmp_path / "workspace")
    with pytest.raises(ValueError):
        store.register_dataset(source, identity_evidence=evidence)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dataset_asset_bindings").fetchone()[0] == 0


def test_unmapped_auxiliary_symbol_is_rejected(tmp_path):
    source = dataset_case(tmp_path / "source")
    evidence = evidence_for(source)
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["auxiliary_tables"] = {"signals": "signals.csv"}
    (source / "manifest.json").write_text(json.dumps(manifest))
    pd.DataFrame(dict(available_at=["2024-01-01T00:00:00Z"], symbol=["foreign"], score=[1])).to_csv(source / "signals.csv", index=False)
    from quanto.data.dataset_identity import declared_dataset_files
    evidence["input_files"] = {name: digest(data) for name, data in declared_dataset_files(source).items()}
    evidence["table_semantics"].append(dict(table="signals", semantic_id="test-signal", version="1", schema_definition={"score": {"dtype": "float", "unit": "score"}}, temporal_rules={"availability_rule": "available_at"}, evidence_ref="fixture:identities"))
    store = WorkbenchStore(tmp_path / "workspace")
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        store.register_dataset(source, identity_evidence=evidence)


def test_publication_failure_leaves_no_registered_dataset_or_binding(tmp_path, monkeypatch):
    source = dataset_case(tmp_path / "source")
    store = WorkbenchStore(tmp_path / "workspace")
    def fail(*args):
        raise RuntimeError("injected binding publication")
    monkeypatch.setattr(BindingStore, "_insert_asset_binding", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.register_dataset(source, identity_evidence=evidence_for(source))
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM dataset_asset_bindings").fetchone()[0] == 0


def register_historical(store, root):
    """Old persisted row; intentionally bypasses the new creation contract."""
    content_hash = digest(json_bytes({name: digest(data) for name, data in source_files(root).items()}))
    with store.transaction() as db:
        db.execute("INSERT INTO datasets VALUES (?,?,?,?)", ("historical", str(root), content_hash, (root / "manifest.json").read_text(encoding="utf-8")))
    return store.get_dataset("historical")


def test_legacy_without_registered_evidence_cannot_guess_identity(tmp_path):
    source = dataset_case(tmp_path / "source")
    store = WorkbenchStore(tmp_path / "workspace")
    version = register_historical(store, source)
    before = source_files(source)
    with pytest.raises(ValueError, match="IDENTITY_UNRESOLVED"):
        BindingStore(store).bind_dataset(version.id, "market:" + "a" * 64, "fake")
    assert source_files(source) == before
    assert store.get_dataset(version.id).content_hash == version.content_hash


def test_binding_rejects_evidence_from_another_object_and_tampered_sidecar(tmp_path):
    source = dataset_case(tmp_path / "source")
    store = WorkbenchStore(tmp_path / "workspace")
    version = store.register_dataset(source, identity_evidence=evidence_for(source))
    binding = BindingStore(store).get_asset_binding(version.id)
    with pytest.raises(ValueError, match="IDENTITY_UNRESOLVED"):
        BindingStore(store).bind_dataset(version.id, "import:" + "0" * 64, "forged")
    (version.path / "identity_evidence.json").write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        BindingStore(store).bind_dataset(version.id, binding.assets[0].evidence_ref, "changed")


def test_superset_and_same_code_different_types_remain_distinct(tmp_path):
    source = dataset_case(tmp_path / "source")
    for name in ("prices", "instruments", "status"):
        frame = pd.read_csv(source / f"{name}.csv", dtype={"symbol": str})
        pd.concat([frame.assign(symbol="fund-local"), frame.assign(symbol="company-local")], ignore_index=True).to_csv(source / f"{name}.csv", index=False)
    fund = InstrumentId(exchange="SZSE", instrument_type="etf", code="000001")
    company = InstrumentId(exchange="SZSE", instrument_type="stock", code="000001")
    bundle = load_dataset(source)
    evidence = make_import_evidence(source,
        assets=tuple(AssetMapping(instrument_id=identity, local_symbol=local, evidence_ref="fixture:identities") for identity, local in [(fund, "fund-local"), (company, "company-local")]),
        benchmark=BenchmarkBinding(custom_series_id="synthetic-benchmark", return_basis="price_index", evidence_ref="fixture:identities"),
        source_document="Two explicitly distinct synthetic identities sharing the same code.",
        table_semantics=standard_table_semantics(bundle, "fixture:identities"))
    store = WorkbenchStore(tmp_path / "workspace")
    version = store.register_dataset(source, identity_evidence=evidence)
    binding = BindingStore(store).get_asset_binding(version.id)
    assert set(load_dataset(version.path).prices.symbol) == {fund.key, company.key}
    assert [item.local_symbol for item in binding.resolve((company,))] == [company.key]
    assert len(binding.assets) == 2


def test_historical_market_binding_keeps_local_keys_and_exact_original_files(tmp_path, monkeypatch):
    from tests.integration.test_market_workbench import prepared_job
    service, _, output, result = prepared_job(tmp_path, monkeypatch)
    bundle = load_dataset(output)
    changes = {}
    for name in ("prices", "instruments", "status", "events"):
        frame = getattr(bundle, name).copy()
        frame["symbol"] = frame.symbol.map({"SZSE:etf:000001": "000001"})
        changes[name] = frame
    legacy_root = tmp_path / "old-local-dataset"
    write_parquet_dataset(replace(bundle, **changes), legacy_root)
    version = register_historical(service.store, legacy_root)
    snapshot = result["market_snapshot_ref"]
    with service.store.transaction() as db:
        db.execute("INSERT INTO dataset_market_sources VALUES (?,?,?)", (version.id, snapshot["id"], snapshot["manifest_digest"]))
    before = source_files(legacy_root)
    binding = BindingStore(service.store).bind_dataset(version.id, f"market:{snapshot['id']}", "legacy-bind")
    assert binding.assets[0].local_symbol == "000001"
    assert binding.assets[0].instrument_id.key == "SZSE:etf:000001"
    assert binding.benchmark.instrument_id.key == "SSE:index:000300"
    assert binding.dataset_content_hash == version.content_hash
    assert source_files(legacy_root) == before
    assert service.store.get_dataset(version.id).content_hash == version.content_hash


def test_market_binding_failure_cannot_publish_partial_dataset(tmp_path, monkeypatch):
    from tests.integration.test_market_workbench import prepared_job
    service, identity, output, result = prepared_job(tmp_path, monkeypatch)
    def fail(*args):
        raise RuntimeError("binding rejected")
    monkeypatch.setattr(BindingStore, "_insert_asset_binding", fail)
    with pytest.raises(RuntimeError, match="binding rejected"):
        service.store.finish_data_job(identity, output, result)
    with pytest.raises(KeyError):
        service.store.get_dataset(identity)
    assert service.store.dataset_market_source(identity) is None
    assert service.store.get_job(identity).state == "running"


def test_import_preserves_leading_zero_auxiliary_instrument_id(tmp_path):
    source = dataset_case(tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["auxiliary_tables"] = {"signals": "signals.csv"}
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "signals.csv").write_text("available_at,instrument_id,score\n2024-01-01T00:00:00Z,000001,1\n")
    evidence = evidence_for(source)
    evidence["table_semantics"].append(dict(table="signals", semantic_id="fixture-signal", version="1",
        schema_definition={"score": {"dtype": "float", "unit": "score"}}, temporal_rules={"availability_rule": "available_at"}, evidence_ref="fixture:identities"))
    store = WorkbenchStore(tmp_path / "workspace")
    version = store.register_dataset(source, identity_evidence=evidence)
    assert load_dataset(version.path).auxiliary["signals"].instrument_id.tolist() == ["SZSE:etf:000001"]


def test_repeated_binding_keeps_frozen_semantics_instead_of_reinterpreting(tmp_path, monkeypatch):
    from tests.integration.test_market_workbench import prepared_job
    import quanto.data.dataset_identity as identity
    service, dataset_id, output, result = prepared_job(tmp_path, monkeypatch)
    assert service.store.finish_data_job(dataset_id, output, result)
    store = BindingStore(service.store)
    binding = store.get_asset_binding(dataset_id)
    def future_schema(*args):
        raise AssertionError("must not replace frozen table semantics")
    monkeypatch.setattr(identity, "standard_table_semantics", future_schema)
    assert store.bind_dataset(dataset_id, binding.assets[0].evidence_ref, "repeat") == binding


def test_identity_domain_keeps_suspended_asset_without_prices(tmp_path):
    source = dataset_case(tmp_path / "source")
    instruments = pd.read_csv(source / "instruments.csv", dtype={"symbol": str})
    pd.concat([instruments, instruments.assign(symbol="000002")]).to_csv(source / "instruments.csv", index=False)
    status = pd.read_csv(source / "status.csv", dtype={"symbol": str})
    pd.concat([status, status.assign(symbol="000002", status="suspended", can_buy=False, can_sell=False)]).to_csv(source / "status.csv", index=False)
    from tests.fixtures.framework_imports import fixture_identity_evidence
    store = WorkbenchStore(tmp_path / "workspace")
    version = store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    binding = BindingStore(store).get_asset_binding(version.id)
    assert {item.local_symbol for item in binding.assets} == {"SZSE:etf:000001", "SZSE:etf:000002"}
    assert set(load_dataset(version.path).prices.symbol) == {"SZSE:etf:000001"}


@pytest.mark.parametrize("table", ["prices", "instruments"])
def test_unknown_identity_in_extended_core_columns_cannot_turn_into_null(tmp_path, table):
    source = dataset_case(tmp_path / "source")
    frame = pd.read_csv(source / f"{table}.csv", dtype={"symbol": str})
    frame["instrument_id"] = "unknown"
    frame.to_csv(source / f"{table}.csv", index=False)
    store = WorkbenchStore(tmp_path / "workspace")
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        store.register_dataset(source, identity_evidence=evidence_for(source))


def test_auxiliary_name_cannot_hide_behind_core_table_semantics(tmp_path):
    source = dataset_case(tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["auxiliary_tables"] = {"prices": "signals.csv"}
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "signals.csv").write_text("available_at,symbol,score\n2024-01-01T00:00:00Z,000001,1\n")
    store = WorkbenchStore(tmp_path / "workspace")
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        store.register_dataset(source, identity_evidence=evidence_for(source))
