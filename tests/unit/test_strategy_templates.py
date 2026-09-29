from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from quanto.strategies.base import Hold, PortfolioView, StrategyContext, TargetPortfolio
from quanto.strategies.buy_hold import BuyHold
from quanto.strategies.trend_rotation import TrendRotation


def _context(prices: dict[str, list[float]], *, held: tuple[str, ...] = (), week_end: bool = True) -> StrategyContext:
    dates = pd.bdate_range("2024-01-01", periods=len(next(iter(prices.values()))))
    rows = [{"date": date, "symbol": symbol, "close": close} for symbol, values in prices.items() for date, close in zip(dates, values)]
    portfolio = PortfolioView(100, 0, {symbol: 1 for symbol in held}, {symbol: 1 for symbol in held}, {symbol: 1 for symbol in held}, 100 + len(held), 0)
    return StrategyContext(dates[-1], pd.DataFrame(rows), tuple(prices), portfolio, (), {}, week_end)


def test_buy_hold_emits_once():
    strategy = BuyHold()
    first_ctx = _context({"A": [1], "B": [1]})
    later_ctx = _context({"A": [1], "B": [1]})
    first = strategy.on_close(first_ctx)
    assert isinstance(first, TargetPortfolio)
    assert first.weights == {"A": 0.5, "B": 0.5}
    assert isinstance(strategy.on_close(later_ctx), Hold)


def test_trend_retains_and_refills():
    strategy = TrendRotation(trend_window=2, momentum_window=1, entry_rank=2, retain_rank=3, evaluation="daily")
    ctx = _context({"A": [10, 11, 12], "B": [10, 12, 13], "C": [10, 11, 14], "D": [10, 10, 9]}, held=("A", "D"))
    target = strategy.on_close(ctx)
    assert target.weights == {"A": 0.5, "C": 0.5}
    assert "D" not in target.weights
    assert strategy.on_close(_context({"A": [10, 9, 8], "B": [10, 9, 8]})).weights == {}
    empty_eligibility = _context({"A": [10, 11, 12]})
    empty_eligibility = StrategyContext(empty_eligibility.asof, empty_eligibility.history, (), empty_eligibility.portfolio, (), {}, True)
    assert strategy.on_close(empty_eligibility).weights == {}


def test_trend_ties_use_symbol_and_each_parameter_changes_behavior():
    prices = {"B": [10, 11, 12], "A": [10, 11, 12], "C": [10, 9, 8]}
    base = TrendRotation(2, 1, 1, 1, "daily")
    assert base.on_close(_context(prices)).weights == {"A": 1.0}
    assert TrendRotation(2, 1, 2, 2, "daily").on_close(_context(prices)).weights == {"A": 0.5, "B": 0.5}
    assert TrendRotation(2, 1, 1, 1, "daily").on_close(_context({"A": [10, 20, 15], "B": [10, 11, 12]})).weights == {"B": 1.0}
    assert TrendRotation(2, 2, 1, 1, "daily").on_close(_context({"A": [10, 20, 15], "B": [10, 11, 12]})).weights == {"A": 1.0}
    assert TrendRotation(2, 1, 1, 1, "daily").on_close(_context({"A": [10, 12, 11], "B": [10, 9, 10]})).weights == {"A": 1.0}
    assert TrendRotation(1, 1, 1, 1, "daily").on_close(_context({"A": [10, 12, 11], "B": [10, 9, 10]})).weights == {"B": 1.0}
    held = _context({"A": [10, 11, 13], "B": [10, 11, 12]}, held=("B",))
    assert TrendRotation(2, 1, 1, 1, "daily").on_close(held).weights == {"A": 1.0}
    assert TrendRotation(2, 1, 1, 2, "daily").on_close(held).weights == {"B": 1.0}
    assert TrendRotation(2, 1, 1, 1, "weekly").should_evaluate(_context(prices, week_end=False)) is False
    assert base.should_evaluate(_context(prices, week_end=False)) is True
    with pytest.raises(ValueError):
        TrendRotation(2, 1, 2, 1, "daily")


def test_templates_use_revision_entrypoint_and_schema():
    root = Path(__file__).resolve().parents[2] / "strategies"
    for name in ("buy_hold", "trend_rotation"):
        manifest = json.loads((root / name / "strategy.json").read_text(encoding="utf-8"))
        assert manifest["entrypoint"] == "strategy.py:create_strategy"
        assert manifest["required_data_columns"] == ["date", "symbol", "close"]
        spec = importlib.util.spec_from_file_location(f"template_{name}", root / name / "strategy.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        strategy = module.create_strategy({})
        assert hasattr(strategy, "on_close") and hasattr(strategy, "should_evaluate")
        with pytest.raises(ValueError):
            module.create_strategy({"unknown": True})
