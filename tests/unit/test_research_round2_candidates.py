from __future__ import annotations

import math

import pandas as pd
import pytest

from quanto.strategies.base import Hold, PortfolioView, StrategyContext, TargetPortfolio
from scripts.research_round2_candidates import BreadthRotation, CashBuyHold, CoreSatellite, DiversifiedTrend


def _context(prices: dict[str, list[float]], *, asof: int | None = None,
             eligible: tuple[str, ...] | None = None, held: tuple[str, ...] = (),
             week_end: bool = True) -> StrategyContext:
    count = max(map(len, prices.values()))
    dates = pd.bdate_range("2024-01-01", periods=count)
    if asof is None:
        asof = count - 1
    rows = [{"date": date, "symbol": symbol, "close": close}
            for symbol, values in prices.items() for date, close in zip(dates, values)]
    portfolio = PortfolioView(100, 0, {symbol: 1 for symbol in held},
                              {symbol: 1 for symbol in held}, {}, 100, 0)
    return StrategyContext(dates[asof], pd.DataFrame(rows), eligible or tuple(prices),
                           portfolio, (), {}, week_end)


def _rising(slope: float, count: int = 221) -> list[float]:
    return [100 + slope * day for day in range(count)]


def test_diversified_trend_keeps_failed_trend_slot_in_cash():
    target = DiversifiedTrend().on_close(_context({"A": _rising(.1), "B": _rising(.2),
                                                    "C": _rising(-.1)}))
    assert isinstance(target, TargetPortfolio)
    assert target.weights == {"A": pytest.approx(.8 / 3), "B": pytest.approx(.8 / 3)}
    assert DiversifiedTrend().warmup_bars == 200


def test_diversified_trend_uses_seasoned_current_positive_prices_only():
    prices = {"A": _rising(.1), "B": _rising(.1)[:200],
              "C": _rising(.1)[:-1], "D": _rising(.1)[:-1] + [math.nan]}
    target = DiversifiedTrend().on_close(_context(prices))
    assert isinstance(target, TargetPortfolio)
    assert target.weights == {"A": pytest.approx(.3)}


def test_inverse_vol_assigns_smaller_slot_to_high_volatility_and_leaves_cash():
    calm = _rising(.1)
    noisy = [100 + .1 * day + (2 if day % 2 else -2) for day in range(221)]
    falling = _rising(-.1)
    target = DiversifiedTrend(weighting="inverse_vol").on_close(
        _context({"A": calm, "B": noisy, "C": falling}))
    assert isinstance(target, TargetPortfolio)
    assert 0 < target.weights["B"] < target.weights["A"] <= .3
    assert sum(target.weights.values()) < .8
    assert DiversifiedTrend(weighting="inverse_vol", trend_window=20).warmup_bars == 60


def test_core_satellite_selects_positive_momentum_top_three_without_retaining_core():
    prices = {"A": _rising(.3, 41), "B": _rising(.2, 41),
              "C": _rising(.1, 41), "D": _rising(-.1, 41)}
    target = CoreSatellite().on_close(_context(prices, held=("D",)))
    assert isinstance(target, TargetPortfolio)
    assert target.weights == {"A": pytest.approx(.23333333333333334),
                              "B": pytest.approx(.23333333333333334),
                              "C": pytest.approx(.23333333333333334),
                              "D": pytest.approx(.1)}
    assert sum(target.weights.values()) == pytest.approx(.8)


def test_core_satellite_omits_short_history_from_satellite_and_caps_weight():
    prices = {"A": _rising(.2, 41), "B": _rising(.2, 41)[:10]}
    target = CoreSatellite(core_weight=.4).on_close(_context(prices, asof=9))
    assert isinstance(target, TargetPortfolio)
    assert target.weights == {"A": pytest.approx(.2), "B": pytest.approx(.2)}
    later = CoreSatellite(core_weight=.4).on_close(_context({"A": _rising(.2, 41)}))
    assert isinstance(later, TargetPortfolio)
    assert later.weights == {"A": pytest.approx(.35)}


def test_breadth_switches_budget_at_half_and_keeps_trend_rotation_selection():
    rising = _rising(.1)
    falling = _rising(-.1)
    half = BreadthRotation(bear_budget=.2).on_close(
        _context({"A": rising, "B": falling}))
    bear = BreadthRotation(bear_budget=.2).on_close(
        _context({"A": rising, "B": falling, "C": falling}))
    assert isinstance(half, TargetPortfolio) and isinstance(bear, TargetPortfolio)
    assert half.weights == {"A": pytest.approx(.35)}
    assert bear.weights == {"A": pytest.approx(.2)}
    assert BreadthRotation().warmup_bars == 200


@pytest.mark.parametrize("strategy", [DiversifiedTrend(), CoreSatellite(), BreadthRotation()])
def test_rotation_candidates_only_evaluate_at_week_end(strategy):
    ctx = _context({"A": _rising(.1)}, week_end=False)
    assert not strategy.should_evaluate(ctx)
    assert isinstance(strategy.on_close(ctx), Hold)


@pytest.mark.parametrize("strategy", [DiversifiedTrend(), CoreSatellite(), BreadthRotation()])
def test_future_rows_do_not_change_historical_signal(strategy):
    past = {"A": _rising(.1), "B": _rising(-.1)}
    future = {"A": past["A"] + [1.0] * 20, "B": past["B"] + [1000.0] * 20}
    before = strategy.on_close(_context(past))
    after = strategy.on_close(_context(future, asof=220))
    assert isinstance(before, TargetPortfolio) and isinstance(after, TargetPortfolio)
    assert before.weights == after.weights


def test_cash_buy_hold_emits_target_once_with_cash_reserve():
    strategy = CashBuyHold()
    ctx = _context({"A": [100], "B": [100]}, week_end=False)
    assert strategy.should_evaluate(ctx)
    first = strategy.on_close(ctx)
    assert isinstance(first, TargetPortfolio)
    assert first.weights == {"A": pytest.approx(.2), "B": pytest.approx(.2)}
    assert not strategy.should_evaluate(ctx)
    assert isinstance(strategy.on_close(ctx), Hold)
    assert strategy.warmup_bars == 0


@pytest.mark.parametrize("factory", [
    lambda: DiversifiedTrend(trend_window=0),
    lambda: DiversifiedTrend(trend_window=True),
    lambda: DiversifiedTrend(gross_target=math.nan),
    lambda: DiversifiedTrend(gross_target=.81),
    lambda: DiversifiedTrend(weighting="risk_parity"),
    lambda: CoreSatellite(core_weight=-.1),
    lambda: CoreSatellite(momentum_window=0),
    lambda: BreadthRotation(trend_window=0),
    lambda: BreadthRotation(bear_budget=math.nan),
    lambda: BreadthRotation(bear_budget=.81),
    lambda: CashBuyHold(gross_target=-.1),
    lambda: CashBuyHold(gross_target=math.nan),
])
def test_invalid_parameters_are_rejected(factory):
    with pytest.raises(ValueError):
        factory()
