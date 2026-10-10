"""Capability checks resolve registered inputs without publishing or training."""
from dataclasses import replace
import json
import socket

import pytest

from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.framework import DefinitionRef
from quanto.investing.definitions import etf_learning_definitions
from quanto.objects import digest
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from quanto.workbench.definitions import DefinitionRegistry
from quanto.workbench.framework_requests import FixedRequest, ModelCreateRequest
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.fixtures.investing_training_cases import investing_case
from tests.integration.test_snapshot_submission import _ready


def _fixed_case(tmp_path):
    store, draft, dataset, strategy = _ready(tmp_path)
    binding = BindingStore(store).get_asset_binding(dataset.id)
    basket = BasketStore(store).create("selected", (binding.assets[0].instrument_id,), "synthetic", "basket")
    values = dict(draft.payload["backtest_spec"])
    request = FixedRequest(framework_contract_version="multi-asset-v1", basket_revision_ref=basket.ref,
        dataset_version_id=dataset.id, strategy_revision_id=strategy.id,
        trading_definition_ref=ETF_TRADING_DEFINITION.ref, strategy_params={},
        backtest_spec=values, sample_purpose="research")
    return store, request


def _training_case(tmp_path):
    source = tmp_path / "source"
    spec, dates = investing_case(source)
    spec["backtest"].update(start=str(dates[65].date()), end=str(dates[99].date()))
    del spec["backtest"]["symbols"]
    spec.update(train_start=str(dates[65].date()), train_end=str(dates[79].date()),
                validation_start=str(dates[80].date()), validation_end=str(dates[89].date()),
                test_start=str(dates[90].date()), test_end=str(dates[99].date()), episode_sessions=10)
    definition = next(item for item in etf_learning_definitions()
                      if item.action_version == "hold-target-cash-quartiles-v2" and item.reward_version == "net-log-return-v1")
    spec.update(action_version=definition.action_version, reward_version=definition.reward_version)
    store = WorkbenchStore(tmp_path / "workbench")
    dataset = store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    binding = BindingStore(store).get_asset_binding(dataset.id)
    basket = BasketStore(store).create("training", tuple(item.instrument_id for item in binding.assets),
                                      "synthetic", "basket")
    request = ModelCreateRequest(framework_contract_version="multi-asset-v1", basket_revision_ref=basket.ref,
        dataset_id=dataset.id, learning_definition_ref=definition.ref,
        trading_definition_ref=ETF_TRADING_DEFINITION.ref, spec=spec)
    return store, request, definition


def test_check_is_offline_read_only_and_check_time_does_not_change_identity(tmp_path, monkeypatch):
    from quanto.workbench.configuration import FrameworkResolver

    store, request = _fixed_case(tmp_path)
    before = {path.relative_to(store.root).as_posix(): digest(path.read_bytes())
              for path in store.root.rglob("*") if path.is_file()}
    def no_network(*args, **kwargs):
        raise AssertionError("Capability checking must not open a connection")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    resolver = FrameworkResolver(store)
    first, second = resolver.check(request), resolver.check(request)
    assert first.status == second.status == "ready"
    assert first.request_hash == second.request_hash
    assert first.generated_at is not None and second.generated_at is not None
    assert before == {path.relative_to(store.root).as_posix(): digest(path.read_bytes())
                      for path in store.root.rglob("*") if path.is_file()}
    assert store.list_jobs() == []


def test_effective_parameters_and_definition_implementation_change_request_hash(tmp_path):
    from quanto.workbench.configuration import FrameworkResolver

    store, request = _fixed_case(tmp_path)
    original = FrameworkResolver(store).check(request)
    params_changed = FrameworkResolver(store).check(request.model_copy(update={"strategy_params": {"window": 3}}))
    registry = DefinitionRegistry()
    definitions = tuple(registry.get(DefinitionRef.model_validate(dict(item["ref"]))) for item in registry.list())
    upgraded = DefinitionRegistry(tuple(replace(item, implementation_hash="e" * 64)
                                         if item.ref == ETF_TRADING_DEFINITION.ref else item for item in definitions))
    implementation_changed = FrameworkResolver(store, upgraded).check(request)
    assert original.status == params_changed.status == implementation_changed.status == "ready"
    assert len({original.request_hash, params_changed.request_hash, implementation_changed.request_hash}) == 3


def test_registered_training_configuration_resolves_all_splits_without_starting_training(tmp_path):
    from quanto.workbench.configuration import FrameworkResolver

    store, request, _ = _training_case(tmp_path)
    resolved = FrameworkResolver(store).resolve(request)
    assert resolved.operation == "train"
    assert resolved.capability_report.status == "ready"
    assert resolved.internal_training_spec["backtest"]["symbols"] == ("SZSE:etf:000001",)
    assert resolved.internal_backtest_spec["symbols"] == ("SZSE:etf:000001",)
    intervals = {(item.interval.start, item.interval.end) for item in resolved.requirements}
    assert (request.spec.train_start, request.spec.train_end) in intervals
    assert (request.spec.validation_start, request.spec.validation_end) in intervals
    assert (request.spec.test_start, request.spec.test_end) in intervals
    assert store.list_jobs() == []


def test_initial_fitted_definition_check_does_not_fit_or_require_an_uncreated_artifact(tmp_path):
    from quanto.workbench.configuration import FrameworkResolver

    store, request, base = _training_case(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not fit or apply a training transform")
    feature = replace(base.feature_definition, transform_recipe="test-fit-v1", fit=forbidden, apply=forbidden)
    learning = replace(base, feature_definition=feature)
    registry = DefinitionRegistry((feature, learning, ETF_TRADING_DEFINITION))
    report = FrameworkResolver(store, registry).check(request)
    assert report.status == "ready", report.model_dump(mode="json")
    assert store.list_jobs() == []


def test_training_rejects_short_history_before_any_model_or_job_is_created(tmp_path):
    from quanto.workbench.configuration import FrameworkResolver

    store, request, _ = _training_case(tmp_path)
    values = request.model_dump(mode="json")
    values["spec"]["backtest"]["start"] = "2024-01-15"
    values["spec"]["train_start"] = "2024-01-15"
    values["spec"]["lookback"] = 61
    report = FrameworkResolver(store).check(ModelCreateRequest.model_validate(values))
    assert report.status == "blocked"
    assert "DATA_REQUIREMENT_UNSATISFIED" in report.error_codes
    assert report.request_hash is not None
    assert store.list_jobs() == []


def test_etf_short_lookback_keeps_its_declared_adaptive_horizons(tmp_path):
    from quanto.workbench.configuration import FrameworkResolver

    store, request, _ = _training_case(tmp_path)
    values = request.model_dump(mode="json")
    values["spec"]["backtest"]["start"] = "2024-01-15"
    values["spec"]["train_start"] = "2024-01-15"
    resolved = FrameworkResolver(store).resolve(ModelCreateRequest.model_validate(values))
    price_requirements = [item for item in resolved.requirements
                          if item.table == "prices" and "etf-learning" in item.requirement_id]
    assert price_requirements
    assert all(item.warmup_sessions == values["spec"]["lookback"] for item in price_requirements)


def test_same_registered_dataset_can_support_prices_but_block_missing_financial_facts(tmp_path):
    from quanto.workbench.configuration import FrameworkBlocked, FrameworkResolver

    store, price_request = _fixed_case(tmp_path)
    original = store.get_strategy(price_request.strategy_revision_id)
    declaration = {**original.manifest, "data_requirements": [*original.manifest["data_requirements"], {
        "requirement_id": "company-profit", "table": "financials", "semantic_version": "1",
        "fields": [{"name": "profit", "dtype": "float", "unit": "CNY", "currency": "CNY"}],
        "scope": "basket", "availability_rule": "timestamp", "price_modes": ["raw_cash"],
        "required": True, "missing_policy": "reject",
    }]}
    files = {path.name: path.read_bytes() for path in original.path.iterdir() if path.is_file()}
    files["strategy.json"] = json.dumps(declaration).encode()
    revision = store.register_strategy(files, declaration)
    financial_request = price_request.model_copy(update={"strategy_revision_id": revision.id})
    resolver = FrameworkResolver(store)
    price_check, financial_check = resolver.check(price_request), resolver.check(financial_request)
    assert price_check.status == "ready"
    assert financial_check.status == "blocked"
    assert financial_check.error_codes == ("DATA_REQUIREMENT_UNSATISFIED",)
    assert financial_check.request_hash is not None
    assert any(item.field_path.endswith(".table") and item.status == "blocked" for item in financial_check.checks)
    with pytest.raises(FrameworkBlocked) as caught:
        resolver.resolve(financial_request)
    assert caught.value.report.request_hash == financial_check.request_hash
    assert store.list_jobs() == []
