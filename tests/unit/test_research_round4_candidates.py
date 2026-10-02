from __future__ import annotations

import math

import pandas as pd
import pytest

from quanto.strategies.base import Hold, PortfolioView, StrategyContext
from scripts.research_round3_candidates import Round3Strategy
from scripts.research_round4_candidates import BreadthRiskStrategy, configurations, families


def _context(prices, *, asof=None, week_end=True, groups=None):
    dates = pd.bdate_range("2023-01-02", periods=max(map(len, prices.values()), default=241))
    asof = dates[-1] if asof is None else dates[asof]
    rows = [{"date": dates[index], "symbol": symbol, "close": price, "amount": 20_000_000}
            for symbol, values in prices.items() for index, price in enumerate(values) if price is not None]
    history = pd.DataFrame(rows, columns=["date", "symbol", "close", "amount"])
    portfolio = PortfolioView(1_000_000, 0, {}, {}, {}, 1_000_000, 0)
    return StrategyContext(asof, history, tuple(prices), portfolio, (),
                           {"symbol_groups": groups or {symbol: symbol for symbol in prices}}, week_end)


def _rise(slope, count=241):
    return [100 + slope * day for day in range(count)]


def _breadth_context(up, total=4, *, week_end=True):
    return _context({f"G{index}": _rise(.2 if index < up else -.2)
                     for index in range(total)}, week_end=week_end)


def test_configuration_families_cover_ten_valid_distinct_candidates():
    specs = configurations()
    names = families()
    assert names == {
        "hysteresis": ["h55_45", "h55_40", "h60_45", "h60_40"],
        "volatility": ["vol10", "vol12", "vol15"],
        "combined": ["h55_45_vol10", "h55_45_vol12", "h55_45_vol15"],
    }
    assert set(specs) == {name for family in names.values() for name in family}
    assert all(spec["kind"] == "breadth_risk" for spec in specs.values())
    assert all(BreadthRiskStrategy(**spec["params"]).warmup_bars >= 200
               for spec in specs.values())
    assert specs["h60_40"]["params"] == {"entry_breadth": .6, "exit_breadth": .4}
    assert specs["vol12"]["params"] == {"target_vol": .12}
    assert specs["h55_45_vol15"]["params"] == {
        "entry_breadth": .55, "exit_breadth": .45, "target_vol": .15}


def test_hysteresis_preserves_band_state_with_boundary_equality_and_resets():
    strategy = BreadthRiskStrategy(entry_breadth=.75, exit_breadth=.5)
    assert strategy.on_close(_breadth_context(2)).weights == {}
    assert strategy.on_close(_breadth_context(3)).weights == {
        "G0": pytest.approx(.16), "G1": pytest.approx(.16), "G2": pytest.approx(.16)}
    assert strategy.on_close(_breadth_context(2)).weights == {
        "G0": pytest.approx(.16), "G1": pytest.approx(.16)}
    assert strategy.on_close(_breadth_context(1)).weights == {}
    assert strategy.on_close(_breadth_context(2)).weights == {}
    assert BreadthRiskStrategy(entry_breadth=.5, exit_breadth=.5).on_close(
        _breadth_context(2)).weights == {"G0": pytest.approx(.16), "G1": pytest.approx(.16)}


def test_nonweekly_hold_and_no_groups_reset_signal_state():
    strategy = BreadthRiskStrategy(entry_breadth=.75, exit_breadth=.5)
    strategy.on_close(_breadth_context(3))
    assert isinstance(strategy.on_close(_breadth_context(1, week_end=False)), Hold)
    assert strategy.on_close(_breadth_context(2)).weights == {
        "G0": pytest.approx(.16), "G1": pytest.approx(.16)}
    assert strategy.on_close(_context({})).weights == {}
    assert strategy.on_close(_breadth_context(2)).weights == {}


def test_unmodified_gate_matches_original_and_keeps_five_fixed_slots():
    for count in (0, 2, 4, 6):
        prices = {f"G{index}": _rise(.2 if index < count else -.2)
                  for index in range(6)}
        ctx = _context(prices)
        assert BreadthRiskStrategy().on_close(ctx).weights == Round3Strategy(
            "breadth_timing", 1).on_close(ctx).weights
    prices = {f"G{index}": _rise(.2) for index in range(2)}
    assert BreadthRiskStrategy(gross_target=.6).on_close(_context(prices)).weights == {
        "G0": pytest.approx(.12), "G1": pytest.approx(.12)}


def test_basket_volatility_uses_covariance_and_fixed_slot_cash():
    a, b = _rise(.1, 181), _rise(.1, 181)
    for index in range(60):
        a.append(a[-1] * (1.04 if index % 2 == 0 else 1.0))
        b.append(b[-1] * (1.0 if index % 2 == 0 else 1.02))
    target = .0008 * math.sqrt(252)
    result = BreadthRiskStrategy(target_vol=target).on_close(_context({"A": a, "B": b}))
    assert result.weights == {"A": pytest.approx(.08), "B": pytest.approx(.08)}
    assert result.diagnostics["basket_volatility"] == pytest.approx(.0016 * math.sqrt(252))


def test_zero_volatility_does_not_leverage_and_missing_grid_date_goes_to_cash():
    flat_return = [100] * 181
    a, b = flat_return.copy(), flat_return.copy()
    for _ in range(60):
        a.append(a[-1] * 1.01)
        b.append(b[-1] * 1.01)
    result = BreadthRiskStrategy(target_vol=.1).on_close(_context({"A": a, "B": b}))
    assert result.weights == {"A": pytest.approx(.16), "B": pytest.approx(.16)}
    missing = a.copy()
    missing[-20] = None
    result = BreadthRiskStrategy(target_vol=.1).on_close(_context({"A": missing, "B": b}))
    assert result.weights == {}
    assert result.diagnostics["risk_history"] == "unavailable"


def test_future_rows_cannot_change_signal_state_or_risk_size():
    a, b = _rise(.2), _rise(.1)
    past = _context({"A": a, "B": b})
    future = _context({"A": a + [1] * 20, "B": b + [1000] * 20}, asof=240)
    before = BreadthRiskStrategy(target_vol=.1).on_close(past)
    after = BreadthRiskStrategy(target_vol=.1).on_close(future)
    assert before.weights == after.weights
    assert before.diagnostics["basket_volatility"] == after.diagnostics["basket_volatility"]


@pytest.mark.parametrize("params", [
    {"entry_breadth": -0.1}, {"entry_breadth": math.nan},
    {"exit_breadth": 1.1}, {"entry_breadth": .4, "exit_breadth": .5},
    {"target_vol": 0}, {"target_vol": math.inf},
    {"vol_window": 0}, {"vol_window": 1.5}, {"vol_window": True},
    {"gross_target": -.1}, {"gross_target": .9},
])
def test_invalid_strategy_settings_are_rejected(params):
    with pytest.raises(ValueError):
        BreadthRiskStrategy(**params)
