from __future__ import annotations

import pandas as pd
import pytest
from datetime import date, datetime, timezone
from types import SimpleNamespace

from quanto.data.contracts import InstrumentId
from quanto.framework import FrozenBasketRevision, ResearchInterval

from quanto.strategies.base import FixedStrategyDefinition, Hold, PortfolioView, StrategyContext, TargetPortfolio, validate_intent, validate_parameters


def _portfolio() -> PortfolioView:
    return PortfolioView(cash=100, receivables=0, shares={}, sellable_shares={}, market_values={}, equity=100, stale_value=0)


def test_strategy_contract_rejects_invalid_input():
    schema = {"type": "object", "properties": {"window": {"type": "integer", "minimum": 1, "default": 4}}}
    assert validate_parameters(schema, {}) == {"window": 4}
    with pytest.raises(ValueError):
        validate_parameters(schema, {"unknown": 1})
    for weights, known in [({"A": float("nan")}, {"A"}), ({"A": -0.1}, {"A"}), ({"A": 0.6, "B": 0.5}, {"A", "B"}), ({"missing": 1}, {"A"})]:
        with pytest.raises(ValueError):
            validate_intent(TargetPortfolio(weights, {}, {}), known)
    with pytest.raises(ValueError):
        validate_intent(TargetPortfolio([("A", 0.5), ("A", 0.5)], {}, {}), {"A"})
    assert isinstance(Hold("wait"), Hold)
    assert TargetPortfolio({}, {}, {}).weights == {}


@pytest.mark.parametrize("datetime_dates", [True, False])
def test_context_excludes_future_and_is_read_only(datetime_dates):
    dates = ["2024-01-02", "2024-01-03"]
    source = pd.DataFrame({"date": pd.to_datetime(dates) if datetime_dates else dates,
                           "symbol": ["A", "A"], "close": [1, 999]})
    ctx = StrategyContext(pd.Timestamp("2024-01-02"), source, ("A",), _portfolio(), ({"symbol": "A", "quantity": 1},), {}, False)
    assert ctx.history["close"].tolist() == [1]
    source.loc[0, "close"] = 50
    assert ctx.history["close"].tolist() == [1]
    with pytest.raises(TypeError):
        ctx.portfolio.shares["A"] = 1
    with pytest.raises(TypeError):
        ctx.pending_orders[0]["quantity"] = 2


def test_preflight_checks_warmup_without_guessing_custom_schedule():
    from quanto.strategies.base import validate_warmup_window

    class ConditionalStrategy:
        warmup_bars = 3

        def should_evaluate(self, ctx):
            raise AssertionError("preflight cannot evaluate a synthetic live context")

        def on_close(self, ctx):
            return Hold("wait")

    strategy = ConditionalStrategy()
    calendar = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    with pytest.raises(ValueError):
        validate_warmup_window(strategy, calendar, pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03"))
    validate_warmup_window(strategy, calendar, pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-04"))


def test_week_end_requires_a_known_later_session():
    from quanto.strategies.base import is_week_end

    full_calendar = pd.bdate_range("2024-01-01", "2024-01-09")
    assert is_week_end(full_calendar, pd.Timestamp("2024-01-03")) is False
    assert is_week_end(full_calendar, pd.Timestamp("2024-01-05")) is True
    assert is_week_end(full_calendar, pd.Timestamp("2024-01-09")) is False
    assert is_week_end(full_calendar[:3], pd.Timestamp("2024-01-03")) is False
    assert is_week_end(full_calendar[:5], pd.Timestamp("2024-01-05")) is False


def test_nested_risk_facts_and_order_summaries_are_read_only():
    approval = {"approval_id": "ok", "pause_asof": "2024-01-01", "available_at": "2024-01-02T06:00:00Z"}
    state = {"symbol_groups": {"A": "G"}, "risk_approvals": [approval]}
    ctx = StrategyContext(pd.Timestamp("2024-01-02"), pd.DataFrame(columns=["date"]), ("A",),
        _portfolio(), ({"metadata": {"source": "risk"}},), state, False)
    with pytest.raises(TypeError):
        ctx.risk_state["symbol_groups"]["A"] = "bypass"
    with pytest.raises(TypeError):
        ctx.risk_state["risk_approvals"][0]["pause_asof"] = "2024-01-02"
    with pytest.raises(AttributeError):
        ctx.risk_state["risk_approvals"].append(approval)
    with pytest.raises(TypeError):
        ctx.pending_orders[0]["metadata"]["source"] = "strategy"
    approval["pause_asof"] = "2024-01-05"
    assert ctx.risk_state["risk_approvals"][0]["pause_asof"] == "2024-01-01"


def test_fixed_definition_freezes_manifest_and_resolves_parameterized_requirements():
    manifest = {
        "name": "example", "entrypoint": "strategy.py:create_strategy",
        "supported_instrument_types": ["etf"],
        "parameter_schema": {"type": "object", "additionalProperties": False,
                             "properties": {"window": {"type": "integer", "minimum": 1, "default": 3}}},
        "data_requirements": [{"requirement_id": "daily-close", "table": "prices", "semantic_version": "1",
                               "fields": [{"name": "close", "dtype": "float", "unit": "CNY", "currency": "CNY"}],
                               "scope": "basket", "availability_rule": "session_close",
                               "price_modes": ["raw_cash", "adjusted_research"], "required": True,
                               "missing_policy": "reject", "warmup_parameters": ["window"]}],
        "feature_set_ref": None, "feature_param_map": {},
        "allowed_trading_refs": [{"kind": "trading", "id": "etf-daily", "version": "1"}],
    }
    definition = FixedStrategyDefinition(manifest, "strategy.py:create_strategy", "a" * 64)
    basket = FrozenBasketRevision(basket_id="test", revision=1,
        members=(InstrumentId(exchange="SSE", instrument_type="etf", code="510300"),),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), selection_note="fixture")
    interval = ResearchInterval(start=date(2024, 1, 2), end=date(2024, 1, 10), warmup_start=date(2023, 12, 1))
    requirements = definition.resolve_requirements({"window": 5}, basket, interval)
    assert len(requirements) == 1
    assert requirements[0].warmup_sessions == 5
    assert requirements[0].interval == interval
    manifest["data_requirements"][0]["fields"][0]["name"] = "changed"
    assert definition.resolve_requirements({}, basket, interval)[0].fields[0].name == "close"
    with pytest.raises(ValueError):
        definition.resolve_requirements({"unknown": 1}, basket, interval)


def test_context_validates_supplied_market_metadata_and_freezes_instruments():
    asof = pd.Timestamp("2024-01-02")
    cutoff = datetime(2024, 1, 2, 7, tzinfo=timezone.utc)
    view = SimpleNamespace(asof=asof.date(), signal_cutoff=cutoff, ordered_symbols=("A",),
                           dataset_content_hash="a" * 64, binding_hash="b" * 64)
    features = SimpleNamespace(asof=asof.date(), signal_cutoff=cutoff, ordered_symbols=("A",),
                               dataset_content_hash="a" * 64, binding_hash="b" * 64)
    instruments = {"A": {"instrument_id": "SSE:etf:510300", "metadata": {"lot_size": 100}}}
    ctx = StrategyContext(asof, pd.DataFrame({"date": [asof], "symbol": ["A"]}),
        ("A",), _portfolio(), (), {}, False, data_view=view, features=features, instruments=instruments)
    instruments["A"]["metadata"]["lot_size"] = 1
    assert ctx.instruments["A"]["metadata"]["lot_size"] == 100
    with pytest.raises(TypeError):
        ctx.instruments["A"]["metadata"]["lot_size"] = 1
    with pytest.raises(ValueError, match="cutoff"):
        StrategyContext(asof, pd.DataFrame({"date": [asof]}), ("A",), _portfolio(), (), {}, False,
            data_view=view, features=SimpleNamespace(asof=asof.date(), signal_cutoff=datetime(2024, 1, 2, 8, tzinfo=timezone.utc),
                ordered_symbols=("A",), dataset_content_hash="a" * 64, binding_hash="b" * 64))
    with pytest.raises(ValueError, match="dataset"):
        StrategyContext(asof, pd.DataFrame({"date": [asof]}), ("A",), _portfolio(), (), {}, False,
            data_view=view, features=SimpleNamespace(asof=asof.date(), signal_cutoff=cutoff,
                ordered_symbols=("A",), dataset_content_hash="c" * 64, binding_hash="b" * 64))
    with pytest.raises(ValueError, match="binding"):
        StrategyContext(asof, pd.DataFrame({"date": [asof]}), ("A",), _portfolio(), (), {}, False,
            data_view=view, features=SimpleNamespace(asof=asof.date(), signal_cutoff=cutoff,
                ordered_symbols=("A",), dataset_content_hash="a" * 64, binding_hash="c" * 64))


def test_fixed_definition_requires_explicit_shared_feature_choice():
    manifest = {
        "entrypoint": "strategy.py:create_strategy", "supported_instrument_types": ["etf"],
        "parameter_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        "data_requirements": [{"requirement_id": "prices"}],
        "allowed_trading_refs": [{"kind": "trading", "id": "etf-daily", "version": "1"}],
    }
    with pytest.raises(ValueError, match="feature_set_ref"):
        FixedStrategyDefinition(manifest, manifest["entrypoint"], "a" * 64)


def test_fixed_definition_accepts_required_parameter_without_default():
    from quanto.config import validate_parameter_schema
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"window": {"type": "integer", "minimum": 1}}, "required": ["window"]}
    assert validate_parameter_schema(schema)["required"] == ["window"]
    manifest = {
        "entrypoint": "strategy.py:create_strategy", "supported_instrument_types": ["etf"],
        "parameter_schema": schema,
        "data_requirements": [{"requirement_id": "prices", "table": "prices", "semantic_version": "1",
                               "fields": [{"name": "close", "dtype": "float", "unit": "price", "currency": "CNY"}],
                               "scope": "basket", "availability_rule": "session_close",
                               "price_modes": ["raw_cash"], "required": True, "missing_policy": "reject"}],
        "feature_set_ref": None, "feature_param_map": {},
        "allowed_trading_refs": [{"kind": "trading", "id": "etf-daily", "version": "1"}],
    }
    definition = FixedStrategyDefinition(manifest, manifest["entrypoint"], "a" * 64)
    assert definition.parameter_schema["required"] == ("window",)
