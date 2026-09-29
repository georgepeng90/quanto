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


def test_preflight_requires_an_evaluable_session():
    from quanto.strategies.base import validate_evaluation_window
    from quanto.strategies.trend_rotation import TrendRotation

    strategy = TrendRotation(trend_window=3, momentum_window=2, entry_rank=1, retain_rank=1, evaluation="weekly")
    calendar = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    with pytest.raises(ValueError):
        validate_evaluation_window(strategy, calendar, pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-04"))
    validate_evaluation_window(strategy, calendar, pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-05"))
