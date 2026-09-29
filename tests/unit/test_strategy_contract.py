from __future__ import annotations

import pandas as pd
import pytest

from quanto.strategies.base import Hold, PortfolioView, StrategyContext, TargetPortfolio, validate_intent, validate_parameters


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


def test_context_excludes_future_and_is_read_only():
    source = pd.DataFrame({"date": pd.to_datetime(["2024-01-02", "2024-01-03"]), "symbol": ["A", "A"], "close": [1, 999]})
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
