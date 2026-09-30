from __future__ import annotations

import math

import pandas as pd

from quanto.strategies.base import Hold, PortfolioView, StrategyContext, TargetPortfolio
from scripts.research_rotation_candidates import GuardedRotation


def _context(prices: dict[str, list[float]], groups: dict[str, str], *, asof: int = 80,
             week_end: bool = True) -> StrategyContext:
    dates = pd.bdate_range("2024-01-01", periods=len(next(iter(prices.values()))))
    rows = [{"date": date, "symbol": symbol, "close": close}
            for symbol, values in prices.items() for date, close in zip(dates, values)]
    portfolio = PortfolioView(100, 0, {}, {}, {}, 100, 0)
    return StrategyContext(dates[asof], pd.DataFrame(rows), tuple(prices), portfolio, (),
                           {"symbol_groups": groups}, week_end)


def _rising(start: float, slope: float, count: int = 81) -> list[float]:
    return [start + slope * day for day in range(count)]


def test_group_deduplication_and_stable_tie_break():
    prices = {"B": _rising(10, .05), "A": _rising(10, .05),
              "C": _rising(10, .04), "D": _rising(10, .03)}
    ctx = _context(prices, {"A": "one", "B": "one", "C": "two", "D": "three"})
    target = GuardedRotation(trend_window=60, momentum_window=20).on_close(ctx)
    assert isinstance(target, TargetPortfolio)
    assert list(target.weights) == ["A", "C", "D"]
    assert sum(target.weights.values()) <= .7


def test_negative_trend_goes_to_cash():
    ctx = _context({"A": _rising(10, -.05)}, {"A": "one"})
    target = GuardedRotation(trend_window=60, momentum_window=20).on_close(ctx)
    assert isinstance(target, TargetPortfolio)
    assert target.weights == {}


def test_higher_realized_volatility_reduces_budget():
    calm = _rising(10, .02)
    noisy = [10 + .02 * day + (.6 if day % 2 == 0 else -.6) for day in range(81)]
    strategy = GuardedRotation(trend_window=60, momentum_window=20)
    calm_target = strategy.on_close(_context({"A": calm}, {"A": "one"}))
    noisy_target = strategy.on_close(_context({"A": noisy}, {"A": "one"}))
    assert isinstance(calm_target, TargetPortfolio) and isinstance(noisy_target, TargetPortfolio)
    assert 0 < noisy_target.weights["A"] < calm_target.weights["A"]


def test_sparse_candidates_leave_unfilled_budget():
    one = GuardedRotation(trend_window=60, momentum_window=20).on_close(
        _context({"A": _rising(10, .02)}, {"A": "one"}))
    assert isinstance(one, TargetPortfolio)
    assert 0 < sum(one.weights.values()) <= .7 / 3 + 1e-12
    assert all(weight <= .3 for weight in one.weights.values())


def test_future_history_cannot_change_past_signal():
    past = _rising(10, .02)
    future = past + [1.0] * 20
    strategy = GuardedRotation(trend_window=60, momentum_window=20)
    first = strategy.on_close(_context({"A": past}, {"A": "one"}))
    extended = strategy.on_close(_context({"A": future}, {"A": "one"}))
    assert isinstance(first, TargetPortfolio) and isinstance(extended, TargetPortfolio)
    assert first.weights == extended.weights


def test_pullback_selection_and_weekly_schedule():
    prices = {"A": _rising(10, .05), "B": _rising(10, .03)}
    strategy = GuardedRotation(trend_window=60, momentum_window=20, selection="pullback", entry_rank=1)
    assert strategy.on_close(_context(prices, {"A": "a", "B": "b"})).weights.keys() == {"B"}
    assert isinstance(strategy.on_close(_context(prices, {"A": "a", "B": "b"}, week_end=False)), Hold)
    assert strategy.warmup_bars == 60
    assert math.isclose(strategy.target_volatility, .12)
