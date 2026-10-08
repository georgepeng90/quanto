from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from types import MappingProxyType

import pandas as pd
import pytest

from quanto.data.contracts import InstrumentId
from quanto.framework import FrozenBasketRevision, ResearchInterval
from quanto.backtest.trading import ETF_TRADING_DEFINITION


def _basket():
    return FrozenBasketRevision(
        basket_id="etf-basket", revision=1,
        members=(InstrumentId(exchange="SSE", instrument_type="etf", code="510300"),),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), selection_note="fixture")


def _fee_input(**changes):
    return {"quantity": 100, "price": 2.0, "commission_rate": 0.0003,
            "minimum_commission": 5.0, **changes}


def _event(kind="dividend"):
    return pd.DataFrame([{"symbol": "SSE:etf:510300", "event_type": kind,
                          "record_date": pd.Timestamp("2024-01-02") if kind == "dividend" else pd.NaT,
                          "ex_date": pd.Timestamp("2024-01-03"),
                          "pay_date": pd.Timestamp("2024-01-04") if kind == "dividend" else pd.NaT,
                          "cash_per_share": 0.1 if kind == "dividend" else 0.0,
                          "split_ratio": 2.0 if kind == "split" else float("nan")}])


def test_etf_fee_preserves_minimum_and_notional_rate_with_money_rounding():
    definition = ETF_TRADING_DEFINITION
    assert dict(definition.fees(_fee_input())) == {"commission": 5.0}
    assert dict(definition.fees(_fee_input(quantity=1000, price=20.0))) == {"commission": 6.0}
    assert dict(definition.fees(_fee_input(quantity=0))) == {"commission": 0.0}
    assert isinstance(definition.fees(_fee_input()), MappingProxyType)


@pytest.mark.parametrize("changes", [
    {"price": float("nan")}, {"price": 0}, {"quantity": -1},
    {"commission_rate": -0.01}, {"minimum_commission": float("inf")},
    {"tax_rate": 0.1},
])
def test_fee_boundary_rejects_invalid_or_extra_inputs(changes):
    with pytest.raises(ValueError):
        ETF_TRADING_DEFINITION.fees(_fee_input(**changes))


def test_fee_hook_receives_read_only_input_and_output_schema_is_strict():
    def mutate(data):
        data["quantity"] = 7
        return {"commission": 1.0}

    changed = replace(ETF_TRADING_DEFINITION, fee_hook=mutate)
    with pytest.raises(TypeError):
        changed.fees(_fee_input())

    for output in ({"commission": -1.0}, {"commission": float("nan")},
                   {"commission": 1.0, "tax": 0.1}, {"fee": 1.0}):
        changed = replace(ETF_TRADING_DEFINITION, fee_hook=lambda _: output)
        with pytest.raises(ValueError):
            changed.fees(_fee_input())


def test_etf_eligibility_requires_observed_state_and_preserves_side_flags():
    definition = ETF_TRADING_DEFINITION
    assert dict(definition.eligibility({"status": "trading", "can_buy": True,
                                        "can_sell": False})) == {
        "can_buy": True, "can_sell": False, "reason": "cannot_sell"}
    assert dict(definition.eligibility({"status": "suspended", "can_buy": False,
                                        "can_sell": False})) == {
        "can_buy": False, "can_sell": False, "reason": "suspended"}
    for state in ({}, {"can_buy": True, "can_sell": True},
                  {"status": "unknown", "can_buy": True, "can_sell": True},
                  {"status": "trading", "can_buy": True},
                  {"status": "trading", "can_buy": 1, "can_sell": True},
                  {"status": "suspended", "can_buy": True, "can_sell": False}):
        with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_MISSING"):
            definition.eligibility(state)


def test_eligibility_hook_is_read_only_and_output_schema_is_strict():
    def mutate(state):
        state["can_buy"] = False
        return {"can_buy": True, "can_sell": True, "reason": ""}

    changed = replace(ETF_TRADING_DEFINITION, eligibility_hook=mutate)
    with pytest.raises(TypeError):
        changed.eligibility({"status": "trading", "can_buy": True, "can_sell": True})
    changed = replace(ETF_TRADING_DEFINITION, eligibility_hook=lambda _: {"can_buy": 1, "can_sell": True, "reason": ""})
    with pytest.raises(ValueError):
        changed.eligibility({"status": "trading", "can_buy": True, "can_sell": True})


def test_eligibility_hook_cannot_mutate_nested_state():
    state = {"status": "trading", "can_buy": True, "can_sell": True,
             "evidence": {"source": ["observed"]}}

    def mutate(value):
        value["evidence"]["source"].append("invented")
        return {"can_buy": True, "can_sell": True, "reason": ""}

    changed = replace(ETF_TRADING_DEFINITION, eligibility_hook=mutate)
    with pytest.raises((AttributeError, TypeError)):
        changed.eligibility(state)
    assert state["evidence"] == {"source": ["observed"]}


def test_eligibility_hook_cannot_create_permission_denied_by_observed_flags():
    permissive = replace(ETF_TRADING_DEFINITION, eligibility_hook=lambda _: {
        "can_buy": True, "can_sell": True, "reason": ""})
    with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_MISSING"):
        permissive.eligibility({"status": "trading", "can_buy": False, "can_sell": True})
    with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_MISSING"):
        permissive.eligibility({"status": "suspended", "can_buy": False, "can_sell": False})


def test_eligibility_hook_may_tighten_observed_permissions():
    restrictive = replace(ETF_TRADING_DEFINITION, eligibility_hook=lambda _: {
        "can_buy": False, "can_sell": True, "reason": "limited"})
    assert dict(restrictive.eligibility({"status": "trading", "can_buy": True,
                                         "can_sell": True})) == {
        "can_buy": False, "can_sell": True, "reason": "limited"}


def test_events_preserve_raw_facts_but_suppress_adjusted_postings():
    definition = ETF_TRADING_DEFINITION
    raw = definition.events(pd.concat([_event("dividend"), _event("split")], ignore_index=True), price_mode="raw_cash")
    assert tuple(event["event_type"] for event in raw) == ("dividend", "split")
    assert raw[0]["cash_per_share"] == 0.1
    assert raw[1]["split_ratio"] == 2.0
    assert raw[1]["record_date"] is None
    assert raw[1]["cash_per_share"] == 0.0
    with pytest.raises(TypeError):
        raw[0]["event_type"] = "other"
    assert definition.events(_event(), price_mode="adjusted_research") == ()


def test_event_dates_required_for_the_event_that_uses_them():
    missing_ex = _event("split")
    missing_ex.loc[0, "ex_date"] = pd.NaT
    with pytest.raises(ValueError):
        ETF_TRADING_DEFINITION.events(missing_ex, price_mode="raw_cash")
    missing_record = _event("dividend")
    missing_record.loc[0, "record_date"] = pd.NaT
    with pytest.raises(ValueError):
        ETF_TRADING_DEFINITION.events(missing_record, price_mode="raw_cash")
    missing_pay = _event("dividend")
    missing_pay.loc[0, "pay_date"] = pd.NaT
    with pytest.raises(ValueError):
        ETF_TRADING_DEFINITION.events(missing_pay, price_mode="raw_cash")


@pytest.mark.parametrize("price_mode", ["raw_cash", "adjusted_research"])
def test_unknown_exit_event_is_rejected_even_if_adjusted_prices_skip_postings(price_mode):
    with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_MISSING.*delisting"):
        ETF_TRADING_DEFINITION.events(_event("delisting"), price_mode=price_mode)


def test_event_hook_gets_copy_and_must_return_declared_event_shape():
    original = _event()

    def mutate(rows):
        rows.loc[0, "event_type"] = "split"
        return ()

    changed = replace(ETF_TRADING_DEFINITION, event_hook=mutate)
    changed.events(original, price_mode="raw_cash")
    assert original.loc[0, "event_type"] == "dividend"
    changed = replace(ETF_TRADING_DEFINITION, event_hook=lambda _: ({"event_type": "dividend"},))
    with pytest.raises(ValueError):
        changed.events(original, price_mode="raw_cash")


def test_etf_declaration_is_frozen_and_requirements_match_daily_tables():
    definition = ETF_TRADING_DEFINITION
    assert definition.ref.kind == "trading"
    assert definition.ref.id == "etf-daily"
    assert definition.supported_instrument_types == ("etf",)
    assert set(definition.supported_price_modes) == {"raw_cash", "adjusted_research"}
    assert definition.scalable_costs == ("commission_rate", "minimum_commission", "slippage_rate")
    with pytest.raises(FrozenInstanceError):
        definition.order_validity = "forever"

    interval = ResearchInterval(start=date(2024, 1, 3), end=date(2024, 1, 5),
                                warmup_start=date(2024, 1, 2))
    requirements = definition.resolve_requirements({}, _basket(), interval)
    assert {item.table for item in requirements} == {
        "prices", "benchmark", "calendar", "instruments", "status", "events"}
    assert all(item.required and item.missing_policy == "reject" for item in requirements)
    assert all(item.interval == interval for item in requirements)
    with pytest.raises(ValueError):
        definition.resolve_requirements({"commission_rate": 0.1}, _basket(), interval)


def test_trading_definition_can_declare_required_parameters():
    schema = {"type": "object", "properties": {"cost_scale": {"type": "number", "minimum": 0}},
              "required": ["cost_scale"], "additionalProperties": False}
    definition = replace(ETF_TRADING_DEFINITION, parameter_schema=schema)
    interval = ResearchInterval(start=date(2024, 1, 3), end=date(2024, 1, 5),
                                warmup_start=date(2024, 1, 2))
    assert definition.resolve_requirements({"cost_scale": 0.5}, _basket(), interval)
    with pytest.raises(ValueError):
        definition.resolve_requirements({}, _basket(), interval)
