"""Resolver identity and failure boundaries for registered workbench inputs."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from quanto.backtest.dataset_identity import make_import_evidence, standard_table_semantics
from quanto.backtest.dataset_loaders import load_dataset
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetMapping, BenchmarkBinding, DefinitionRef
from quanto.workbench.baskets import BasketStore
from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.framework_requests import (FetchCapabilityRequest, FetchRequest,
                                                 FixedCapabilityRequest, FixedRequest,
                                                 ModelCreateRequest, TrainCapabilityRequest)
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.framework_cases import fixed_request_values, instrument_values
from tests.fixtures.backtest_cases import dataset_case
from tests.integration.test_snapshot_submission import _ready
from tests.integration.test_framework_capabilities import _training_case


def test_missing_references_have_specific_paths_and_no_hash(tmp_path):
    resolver = FrameworkResolver(WorkbenchStore(tmp_path / "workbench"))
    request = FixedRequest.model_validate(fixed_request_values())
    report = resolver.check(request)
    assert report.status == "blocked"
    assert report.request_hash is None
    assert {item.field_path for item in report.checks if item.status == "blocked"} >= {
        "basket_revision_ref", "dataset_version_id", "strategy_revision_id"
    }


def test_resolved_fixed_input_keeps_canonical_order_and_hash(tmp_path):
    store, _, dataset, strategy = _ready(tmp_path)
    basket = BasketStore(store).create("one", (dataset_identity(),), "unit selection", "basket-one")
    values = fixed_request_values()
    values.update(basket_revision_ref=basket.ref.model_dump(mode="json"),
                  dataset_version_id=dataset.id, strategy_revision_id=strategy.id,
                  trading_definition_ref=DefinitionRef(kind="trading", id="etf-daily", version="1").model_dump(mode="json"))
    values["backtest_spec"].update(start="2024-01-01", end="2024-01-03")
    request = FixedRequest.model_validate(values)
    resolver = FrameworkResolver(store)
    report = resolver.check(request)
    assert report.status == "ready"
    assert report.request_hash is not None
    assert report.dataset_version_id == dataset.id
    assert report.request_hash == resolver.check(request).request_hash
    resolved = resolver.resolve(request)
    assert resolved.internal_backtest_spec["symbols"] == ("SZSE:etf:000001",)
    assert resolved.capability_report.request_hash == resolved.request_hash


def dataset_identity():
    return InstrumentId.model_validate(instrument_values())


def test_fetch_precheck_does_not_create_dataset(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench")
    request = FetchRequest.model_validate({"instruments": [instrument_values()],
        "start_date": "2024-01-01", "end_date": "2024-01-03"})
    resolver = FrameworkResolver(store)
    report = resolver.check(request)
    assert report.status == "ready"
    assert report.request_hash is not None
    assert report.dataset_version_id is None
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 0


def test_unknown_definition_is_a_reference_failure(tmp_path):
    store, _, dataset, strategy = _ready(tmp_path)
    basket = BasketStore(store).create("one", (dataset_identity(),), "unit selection", "basket-one")
    values = fixed_request_values()
    values.update(basket_revision_ref=basket.ref.model_dump(mode="json"),
                  dataset_version_id=dataset.id, strategy_revision_id=strategy.id,
                  trading_definition_ref={"kind": "trading", "id": "unknown", "version": "1"})
    report = FrameworkResolver(store).check(FixedRequest.model_validate(values))
    assert report.request_hash is None
    assert report.error_codes == ("DEFINITION_UNAVAILABLE",)
    assert report.checks[0].field_path == "trading_definition_ref"


def test_conflicting_requirement_id_blocks_with_resolved_hash(tmp_path):
    store, _, dataset, strategy = _ready(tmp_path)
    basket = BasketStore(store).create("one", (dataset_identity(),), "unit selection", "basket-one")
    manifest = dict(strategy.manifest)
    manifest["data_requirements"] = [{**manifest["data_requirements"][0],
                                      "requirement_id": "etf-trading-prices"}]
    files = {"strategy.py": b"def create_strategy(params): return None\n",
             "helper.py": b"value = 1\n",
             "strategy.json": json.dumps(manifest).encode()}
    revision = store.register_strategy(files, manifest)
    values = fixed_request_values()
    values.update(basket_revision_ref=basket.ref.model_dump(mode="json"),
                  dataset_version_id=dataset.id, strategy_revision_id=revision.id,
                  trading_definition_ref={"kind": "trading", "id": "etf-daily", "version": "1"})
    values["backtest_spec"].update(start="2024-01-01", end="2024-01-03")
    report = FrameworkResolver(store).check(FixedRequest.model_validate(values))
    assert report.request_hash is not None
    assert "DATA_REQUIREMENT_UNSATISFIED" in report.error_codes
    assert any(check.field_path == "requirements.etf-trading-prices" for check in report.checks)


def test_stock_without_registered_definition_reports_only_definition_unavailable(tmp_path):
    store, _, _, strategy = _ready(tmp_path)
    source = dataset_case(tmp_path / "stock-source")
    bundle = load_dataset(source)
    stock = InstrumentId(exchange="SZSE", instrument_type="stock", code="000001")
    evidence = make_import_evidence(source,
        assets=(AssetMapping(instrument_id=stock, local_symbol="000001", evidence_ref="unit:stock"),),
        benchmark=BenchmarkBinding(custom_series_id="synthetic-benchmark", return_basis="price_index",
                                   evidence_ref="unit:stock"),
        source_document="Explicit synthetic stock identity",
        table_semantics=standard_table_semantics(bundle, "unit:stock"))
    dataset = store.register_dataset(source, identity_evidence=evidence)
    basket = BasketStore(store).create("stock", (stock,), "unit selection", "stock-basket")
    values = fixed_request_values()
    values.update(basket_revision_ref=basket.ref.model_dump(mode="json"),
                  dataset_version_id=dataset.id, strategy_revision_id=strategy.id,
                  trading_definition_ref={"kind": "trading", "id": "etf-daily", "version": "1"})
    values["backtest_spec"].update(start="2024-01-01", end="2024-01-03")
    report = FrameworkResolver(store).check(FixedRequest.model_validate(values))
    assert report.request_hash is not None
    assert report.error_codes == ("DEFINITION_UNAVAILABLE",)


def test_mixed_basket_reports_only_mixed_capability(tmp_path):
    store, _, _, strategy = _ready(tmp_path)
    source = dataset_case(tmp_path / "mixed-source")
    for name in ("prices", "status", "instruments"):
        path = source / f"{name}.csv"
        original = pd.read_csv(path, dtype={"symbol": str})
        other = original.copy()
        other["symbol"] = "000002"
        pd.concat((original, other), ignore_index=True).to_csv(path, index=False)
    bundle = load_dataset(source)
    etf = dataset_identity()
    stock = InstrumentId(exchange="SZSE", instrument_type="stock", code="000002")
    evidence = make_import_evidence(source,
        assets=tuple(AssetMapping(instrument_id=item, local_symbol=item.code, evidence_ref="unit:mixed")
                     for item in (etf, stock)),
        benchmark=BenchmarkBinding(custom_series_id="synthetic-benchmark", return_basis="price_index",
                                   evidence_ref="unit:mixed"),
        source_document="Explicit synthetic mixed identities",
        table_semantics=standard_table_semantics(bundle, "unit:mixed"))
    dataset = store.register_dataset(source, identity_evidence=evidence)
    basket = BasketStore(store).create("mixed", (etf, stock), "unit selection", "mixed-basket")
    values = fixed_request_values()
    values.update(basket_revision_ref=basket.ref.model_dump(mode="json"),
                  dataset_version_id=dataset.id, strategy_revision_id=strategy.id,
                  trading_definition_ref={"kind": "trading", "id": "etf-daily", "version": "1"})
    values["backtest_spec"].update(start="2024-01-01", end="2024-01-03")
    report = FrameworkResolver(store).check(FixedRequest.model_validate(values))
    assert report.request_hash is not None
    assert report.error_codes == ("MIXED_BASKET_UNSUPPORTED",)


def test_selected_member_without_binding_retains_resolved_hash(tmp_path):
    store, _, dataset, strategy = _ready(tmp_path)
    other = InstrumentId(exchange="SZSE", instrument_type="etf", code="000002")
    basket = BasketStore(store).create("missing", (dataset_identity(), other),
                                       "unit selection", "missing-member-basket")
    values = fixed_request_values()
    values.update(basket_revision_ref=basket.ref.model_dump(mode="json"),
                  dataset_version_id=dataset.id, strategy_revision_id=strategy.id,
                  trading_definition_ref={"kind": "trading", "id": "etf-daily", "version": "1"})
    values["backtest_spec"].update(start="2024-01-01", end="2024-01-03")
    report = FrameworkResolver(store).check(FixedRequest.model_validate(values))
    assert report.request_hash is not None
    assert report.error_codes == ("DATA_REQUIREMENT_UNSATISFIED",)
    assert report.checks[0].field_path == "basket_revision_ref.members"


@pytest.mark.parametrize("split,start_index,end_index", [
    ("train", 65, 66), ("validation", 80, 81), ("test", 90, 91),
])
def test_training_partition_needs_natural_executable_decision(tmp_path, split, start_index, end_index):
    store, request, _ = _training_case(tmp_path)
    dates = pd.bdate_range("2024-01-01", periods=100)
    values = request.model_dump(mode="json")
    values["spec"][f"{split}_start"] = dates[start_index].date().isoformat()
    values["spec"][f"{split}_end"] = dates[end_index].date().isoformat()
    if split == "train":
        values["spec"]["episode_sessions"] = 2
    report = FrameworkResolver(store).check(ModelCreateRequest.model_validate(values))
    assert report.status == "blocked"
    assert report.request_hash is not None
    assert "DATA_REQUIREMENT_UNSATISFIED" in report.error_codes
    assert any(check.field_path == f"spec.{split}" for check in report.checks)


def test_training_episode_must_fit_actual_train_sessions(tmp_path):
    store, request, _ = _training_case(tmp_path)
    values = request.model_dump(mode="json")
    values["spec"]["episode_sessions"] = 16  # Registered train partition has 15 sessions.
    report = FrameworkResolver(store).check(ModelCreateRequest.model_validate(values))
    assert report.status == "blocked"
    assert report.request_hash is not None
    assert "DATA_REQUIREMENT_UNSATISFIED" in report.error_codes
    assert any(check.field_path == "spec.episode_sessions" for check in report.checks)


def test_operation_discriminator_does_not_change_fixed_identity(tmp_path):
    store, _, dataset, strategy = _ready(tmp_path)
    basket = BasketStore(store).create("one", (dataset_identity(),), "unit selection", "basket-one")
    values = fixed_request_values()
    values.update(basket_revision_ref=basket.ref.model_dump(mode="json"),
                  dataset_version_id=dataset.id, strategy_revision_id=strategy.id,
                  trading_definition_ref={"kind": "trading", "id": "etf-daily", "version": "1"})
    values["backtest_spec"].update(start="2024-01-01", end="2024-01-03")
    resolver = FrameworkResolver(store)
    plain = FixedRequest.model_validate(values)
    typed = FixedCapabilityRequest.model_validate({**values, "operation": "fixed_backtest"})
    assert resolver.check(plain).request_hash == resolver.check(typed).request_hash
    assert "operation" not in resolver.resolve(typed).request


def test_operation_discriminator_does_not_change_train_identity(tmp_path):
    store, plain, _ = _training_case(tmp_path)
    typed = TrainCapabilityRequest.model_validate({**plain.model_dump(mode="json"), "operation": "train"})
    resolver = FrameworkResolver(store)
    assert resolver.check(plain).request_hash == resolver.check(typed).request_hash
    assert "operation" not in resolver.resolve(typed).request


def test_operation_discriminator_does_not_change_fetch_identity(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench")
    values = {"instruments": [instrument_values()], "start_date": "2024-01-01", "end_date": "2024-01-03"}
    plain = FetchRequest.model_validate(values)
    typed = FetchCapabilityRequest.model_validate({**values, "operation": "fetch"})
    resolver = FrameworkResolver(store)
    assert resolver.check(plain).request_hash == resolver.check(typed).request_hash
