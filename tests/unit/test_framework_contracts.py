from copy import deepcopy
from datetime import datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from quanto.data.contracts import InstrumentId
from tests.fixtures.framework_cases import (
    basket_values, binding_values, fixed_request_values, instrument_values, model_request_values,
)


def test_public_identity_distinguishes_exchange_and_type_without_rewriting_local_symbols():
    from quanto.framework import AssetBinding
    etf = InstrumentId(**instrument_values())
    assert etf.key != InstrumentId(**instrument_values(instrument_type="stock")).key
    assert etf.key != InstrumentId(**instrument_values(exchange="SSE")).key
    binding = AssetBinding(**binding_values())
    assert binding.resolve((etf,))[0].local_symbol == "000001"
    assert binding.dataset_content_hash == "a" * 64


def test_basket_hash_preserves_order_and_rejects_tampered_content():
    from quanto.framework import FrozenBasketRevision
    values = basket_values()
    a = FrozenBasketRevision(**values)
    b = FrozenBasketRevision(**(values | {"members": list(reversed(values["members"]))}))
    assert a.content_hash != b.content_hash
    with pytest.raises(ValidationError, match="content_hash"):
        FrozenBasketRevision(**(values | {"content_hash": b.content_hash}))
    assert FrozenBasketRevision.model_validate_json(a.model_dump_json()) == a


@pytest.mark.parametrize("members", [[], [instrument_values()] * 2, [instrument_values(instrument_type="index")]])
def test_basket_rejects_empty_duplicate_and_nontradable_members(members):
    from quanto.framework import FrozenBasketRevision
    with pytest.raises(ValidationError):
        FrozenBasketRevision(**(basket_values() | {"members": members}))


@pytest.mark.parametrize("revision", [0, -1, True, "1", "latest"])
def test_revision_requires_explicit_positive_integer(revision):
    from quanto.framework import BasketRevisionRef
    with pytest.raises(ValidationError):
        BasketRevisionRef(basket_id="basket-1", revision=revision)


def test_binding_rejects_alias_collisions_and_incomplete_dataset_coverage():
    from quanto.framework import AssetBinding
    values = binding_values()
    for field in ("instrument_id", "local_symbol"):
        conflicting = deepcopy(values)
        conflicting["assets"][1][field] = conflicting["assets"][0][field]
        with pytest.raises(ValidationError, match="unique"):
            AssetBinding(**conflicting)
    binding = AssetBinding(**values)
    binding.validate_symbols(("000002", "000001"))
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        binding.validate_symbols(("000001", "unknown"))
    with pytest.raises(ValueError, match="DATA_REQUIREMENT_UNSATISFIED"):
        binding.resolve((InstrumentId(**instrument_values("000003")),))
    assert [item.local_symbol for item in binding.resolve(tuple(InstrumentId(**instrument_values(code))
                                                              for code in ("000002", "000001")))] == ["000002", "000001"]


def test_binding_hash_freezes_table_semantics_and_nested_source_metadata():
    from quanto.framework import AssetBinding
    values = binding_values()
    binding = AssetBinding(**values)
    before = binding.binding_hash
    values["table_semantics"][0]["temporal_rules"]["keys"].append("future")
    assert binding.table_semantics[0].temporal_rules["keys"] == ("symbol", "date")
    with pytest.raises(TypeError):
        binding.table_semantics[0].schema_definition["close"]["unit"] = "USD"
    newer = AssetBinding(**values)
    assert newer.binding_hash != before
    assert AssetBinding.model_validate_json(binding.model_dump_json()) == binding
    with pytest.raises(ValidationError, match="binding_hash"):
        AssetBinding(**(values | {"binding_hash": before}))


def test_benchmark_role_is_separate_from_trade_asset_aliases():
    from quanto.framework import AssetBinding
    values = binding_values()
    values["benchmark"]["instrument_id"] = instrument_values(instrument_type="index")
    binding = AssetBinding(**values)
    assert len(binding.assets) == 2
    with pytest.raises(ValidationError):
        AssetBinding(**(values | {"benchmark": values["benchmark"] | {"custom_series_id": "custom"}}))


def test_frozen_json_and_hash_are_order_independent_for_maps_but_not_arrays():
    from quanto.framework import canonical_hash, ResolvedDefinition
    assert canonical_hash({"a": 1, "b": [2, 3]}) == canonical_hash({"b": [2, 3], "a": 1})
    assert canonical_hash({"b": [2, 3]}) != canonical_hash({"b": [3, 2]})
    for invalid in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            canonical_hash({"invalid": invalid})
        with pytest.raises(ValidationError):
            ResolvedDefinition(ref=dict(kind="feature", id="test", version="1"), implementation_hash="b" * 64,
                               effective_params={"nested": [invalid]}, declaration={})


def test_time_and_operation_capability_contracts_reject_unfounded_ready_status():
    from quanto.framework import FrozenBasketRevision, ResearchInterval, CapabilityReport
    with pytest.raises(ValidationError, match="timezone"):
        FrozenBasketRevision(**(basket_values() | {"created_at": datetime(2024, 1, 1)}))
    with pytest.raises(ValidationError):
        ResearchInterval(start="2024-01-01", end="2024-02-01", warmup_start="2024-01-02")
    values = dict(operation="train", status="blocked", checks=(), checker_version="1")
    assert CapabilityReport(**values).request_hash is None
    with pytest.raises(ValidationError, match="request_hash"):
        CapabilityReport(**(values | {"status": "ready"}))


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(symbols=["000001"]),
    lambda value: value["backtest_spec"].update(symbols=["000001", "000002"]),
    lambda value: value.update(feature_params={"lookback": 5}),
    lambda value: value.update(framework_contract_version="latest"),
    lambda value: value["trading_definition_ref"].update(kind="learning"),
])
def test_fixed_public_input_rejects_duplicate_authority_and_wrong_definition_kind(mutate):
    from quanto.workbench.framework_requests import FixedRequest
    value = fixed_request_values()
    mutate(value)
    with pytest.raises(ValidationError):
        FixedRequest(**value)


def test_internal_backtest_derives_ordered_symbols_and_translates_canonical_risk_keys():
    from quanto.framework import AssetBinding
    from quanto.workbench.framework_requests import FixedRequest
    values = fixed_request_values()
    values["backtest_spec"]["risk"]["stress_scenarios"] = [dict(name="drawdown",
        symbol_shocks={"SZSE:etf:000002": -.2}, execution_cost_rate=.001)]
    request = FixedRequest(**values)
    binding = AssetBinding(**binding_values())
    pairs = binding.resolve(tuple(InstrumentId(**instrument_values(code)) for code in ("000002", "000001")))
    spec = request.backtest_spec.to_spec(pairs)
    assert spec.symbols == ["000002", "000001"]
    assert spec.risk.stress_scenarios[0].symbol_shocks == {"000002": -.2}
    values["backtest_spec"]["risk"]["stress_scenarios"][0]["symbol_shocks"] = {"000002": -.2}
    with pytest.raises(ValueError, match="canonical"):
        FixedRequest(**values).backtest_spec.to_spec(pairs)


def test_model_input_reuses_current_training_validation_and_never_accepts_symbols():
    from quanto.framework import AssetBinding
    from quanto.workbench.framework_requests import ModelCreateRequest
    values = model_request_values()
    request = ModelCreateRequest(**values)
    pairs = AssetBinding(**binding_values()).assets
    spec = request.spec.to_spec(pairs)
    assert spec.backtest.symbols == ["000001", "000002"]
    assert spec.lookback == 5
    values["spec"]["backtest"]["symbols"] = spec.backtest.symbols
    with pytest.raises(ValidationError):
        ModelCreateRequest(**values)
    values = model_request_values()
    values["spec"]["validation_start"] = "2024-03-01"
    with pytest.raises(ValidationError, match="ordered and disjoint"):
        ModelCreateRequest(**values)


def test_fetch_requires_explicit_typed_members_and_no_existing_dataset():
    from quanto.workbench.framework_requests import FetchRequest
    values = dict(instruments=[instrument_values()], start_date="2024-01-01", end_date="2024-06-30")
    assert FetchRequest(**values).instruments[0].key == "SZSE:etf:000001"
    for change in ({"instruments": ["000001"]}, {"dataset_id": "dataset-1"}, {"symbols": ["000001"]}):
        with pytest.raises(ValidationError):
            FetchRequest(**(values | change))
    with pytest.raises(ValidationError, match="index"):
        FetchRequest(**(values | {"benchmark": instrument_values(instrument_type="stock")}))


def test_model_requests_do_not_offer_editable_research_identity():
    from quanto.workbench.framework_requests import PolicyBacktestRequest, ResumeRequest
    with pytest.raises(ValidationError):
        ResumeRequest(model_id="model-1", additional_timesteps=8, dataset_id="new")
    values = dict(version_id="version-1", dataset_id="dataset-1", spec=dict(start="2024-07-01", end="2024-08-01"))
    with pytest.raises(ValidationError):
        PolicyBacktestRequest(**values)
    values["spec"]["evaluation_purpose"] = "development_replay"
    assert PolicyBacktestRequest(**values).spec.evaluation_purpose == "development_replay"
    values["spec"]["evaluation_purpose"] = "model_selection"
    with pytest.raises(ValidationError):
        PolicyBacktestRequest(**values)


def test_capability_request_has_one_operation_and_uses_same_body_schema():
    from quanto.workbench.framework_requests import CapabilityCheckRequest, FixedRequest
    adapter = TypeAdapter(CapabilityCheckRequest)
    request = adapter.validate_python(dict(operation="fixed_backtest", **fixed_request_values()))
    assert isinstance(request, FixedRequest)
    with pytest.raises(ValidationError):
        adapter.validate_python(dict(operation="fixed_backtest", config=fixed_request_values()))


def test_requirement_fields_and_missing_policy_are_explicit():
    from quanto.framework import RequirementItem
    values = dict(requirement_id="close", table="prices", semantic_version="1",
                  fields=[dict(name="close", dtype="float", unit="CNY", currency="CNY")],
                  scope="basket", interval=dict(start="2024-01-01", end="2024-02-01", warmup_start="2023-12-01"),
                  availability_rule="session_close", price_modes=["raw_cash"], required=True, missing_policy="reject")
    assert RequirementItem(**values).fields[0].currency == "CNY"
    with pytest.raises(ValidationError):
        RequirementItem(**(values | {"missing_policy": "future_fill"}))
    with pytest.raises(ValidationError, match="unique"):
        RequirementItem(**(values | {"fields": values["fields"] * 2}))


def test_resolved_configuration_freezes_internal_json_and_checks_report_correlation():
    from quanto.framework import AssetBinding, FrozenBasketRevision, ResolvedConfiguration
    binding = AssetBinding(**binding_values())
    values = dict(operation="fixed_backtest", request=fixed_request_values(), basket=FrozenBasketRevision(**basket_values()),
                  asset_binding=binding, ordered_pairs=binding.assets, definitions=[], requirements=[],
                  internal_backtest_spec={"symbols": ["000001", "000002"]},
                  request_hash="c" * 64, capability_report=dict(operation="fixed_backtest", status="ready",
                                                              request_hash="c" * 64, checks=[], checker_version="1"))
    resolved = ResolvedConfiguration(**values)
    values["internal_backtest_spec"]["symbols"].reverse()
    assert resolved.internal_backtest_spec["symbols"] == ("000001", "000002")
    with pytest.raises(TypeError):
        resolved.request["backtest_spec"]["initial_cash"] = 1
    with pytest.raises(ValidationError, match="request_hash"):
        ResolvedConfiguration(**(values | {"request_hash": "d" * 64}))


@pytest.mark.parametrize("version", [True, "1", 2])
def test_binding_schema_version_requires_integer_one(version):
    from quanto.framework import AssetBinding
    with pytest.raises(ValidationError):
        AssetBinding(**(binding_values() | {"schema_version": version}))


def test_capability_report_preserves_timezone_aware_check_time():
    from quanto.framework import CapabilityReport
    values = dict(operation="fetch", status="blocked", checks=[], checker_version="1",
                  generated_at="2024-01-01T15:00:00+08:00")
    assert CapabilityReport(**values).generated_at.isoformat() == "2024-01-01T07:00:00+00:00"
    with pytest.raises(ValidationError, match="timezone"):
        CapabilityReport(**(values | {"generated_at": "2024-01-01T15:00:00"}))
