from __future__ import annotations

import math

import pandas as pd
import pytest

from quanto.strategies.base import Hold, PortfolioView, StrategyContext, TargetPortfolio
from scripts.research_round3_candidates import GroupEqual, Round3Strategy, configurations, descriptions


def _context(prices, *, amounts=None, groups=None, asof=None, held=(), week_end=True):
    count = max(map(len, prices.values()))
    dates = pd.bdate_range("2023-01-02", periods=count)
    if asof is None:
        asof = count - 1
    amounts = amounts or {}
    rows = [{"date": date, "symbol": symbol, "close": price,
             "amount": amounts.get(symbol, [20_000_000] * len(values))[index]}
            for symbol, values in prices.items() for index, (date, price) in enumerate(zip(dates, values))]
    portfolio = PortfolioView(1_000_000, 0, {symbol: 100 for symbol in held},
                              {symbol: 100 for symbol in held}, {}, 1_000_000, 0)
    return StrategyContext(dates[asof], pd.DataFrame(rows), tuple(prices), portfolio, (),
                           {"symbol_groups": groups or {symbol: symbol for symbol in prices}}, week_end)


def _rise(slope, count=241):
    return [100 + day * slope for day in range(count)]


def test_twenty_frozen_configurations_are_valid_and_distinct():
    specs = configurations()
    assert len(specs) == 20
    assert len({item["params"]["family"] for item in specs.values()}) == 10
    assert all(item["kind"] == "round3" and item["params"]["variant"] in (0, 1)
               for item in specs.values())
    assert all(Round3Strategy(**item["params"]).warmup_bars >= 120 for item in specs.values())
    assert set(descriptions()) == {item["params"]["family"] for item in specs.values()}


def test_momentum_selects_one_per_group_with_fixed_cash_slots_and_stable_ties():
    prices = {"B": _rise(.4), "A": _rise(.4), "C": _rise(.2), "D": _rise(-.1)}
    target = Round3Strategy("momentum", 0).on_close(_context(prices, groups={
        "A": "tech", "B": "tech", "C": "health", "D": "energy"}))
    assert isinstance(target, TargetPortfolio)
    assert target.weights == {"A": pytest.approx(.16), "C": pytest.approx(.16)}


def test_liquidity_and_stale_or_short_histories_cannot_enter():
    prices = {"A": _rise(.4), "B": _rise(.8), "C": _rise(.9)[:-1], "D": _rise(1)[:100]}
    amounts = {"B": [5_000_000] * 241}
    target = Round3Strategy("momentum", 0).on_close(_context(prices, amounts=amounts))
    assert target.weights == {"A": pytest.approx(.16)}


def test_future_rows_do_not_change_past_signal_or_historical_liquidity():
    past = {"A": _rise(.2), "B": _rise(.1)}
    before = _context(past)
    future = {"A": past["A"] + [1] * 20, "B": past["B"] + [1000] * 20}
    after = _context(future, asof=240, amounts={"B": [20_000_000] * 241 + [0] * 20})
    for family in ("momentum", "breadth_timing", "correlation"):
        strategy = Round3Strategy(family, 0)
        assert strategy.on_close(before).weights == strategy.on_close(after).weights


def test_distinct_signal_families_select_expected_side_of_controlled_paths():
    prices = {"fast": _rise(.3), "slow": _rise(.1), "down": _rise(-.1)}
    ctx = _context(prices)
    assert "fast" in Round3Strategy("momentum", 0).on_close(ctx).weights
    assert "fast" in Round3Strategy("dual_momentum", 0).on_close(ctx).weights
    assert "fast" in Round3Strategy("ma_trend", 0).on_close(ctx).weights
    assert "fast" in Round3Strategy("regression", 0).on_close(ctx).weights
    assert "down" not in Round3Strategy("low_vol", 0).on_close(ctx).weights


def test_breakout_requires_new_high_but_can_retain_a_held_trend():
    base = _rise(.2)
    held = base[:-1] + [base[-2] - .1]
    new_high = base[:-1] + [base[-2] + 1]
    assert Round3Strategy("breakout", 0).on_close(_context({"A": held})).weights == {}
    assert Round3Strategy("breakout", 0).on_close(_context({"A": held}, held=("A",))).weights == {"A": pytest.approx(.16)}
    assert Round3Strategy("breakout", 0).on_close(_context({"A": new_high})).weights == {"A": pytest.approx(.16)}


def test_pullback_picks_dip_only_with_positive_long_trend():
    rising = _rise(.2)
    dip = rising[:-1] + [rising[-2] - 2]
    target = Round3Strategy("pullback", 0).on_close(_context({"dip": dip, "plain": rising}))
    assert target.weights == {"dip": pytest.approx(.16)}


def test_breadth_reduces_budget_when_most_groups_are_below_trend():
    ctx = _context({"A": _rise(.2), "B": _rise(-.2), "C": _rise(-.1)})
    assert Round3Strategy("breadth_timing", 0).on_close(ctx).weights == {"A": pytest.approx(.08)}
    assert Round3Strategy("breadth_timing", 1).on_close(ctx).weights == {}


def test_correlation_constraint_rejects_duplicate_return_path():
    prices = {"A": _rise(.3), "B": _rise(.29),
              "C": [100 + .1 * day + (1 if day % 2 else -1) for day in range(241)]}
    target = Round3Strategy("correlation", 0).on_close(_context(prices))
    assert "A" in target.weights
    assert "B" not in target.weights


def test_weekly_only_and_invalid_settings():
    assert isinstance(Round3Strategy("momentum", 0).on_close(_context({"A": _rise(.2)}, week_end=False)), Hold)
    for args in (("bogus", 0, .8), ("momentum", 2, .8), ("momentum", 0, math.nan),
                 ("momentum", 0, 1.0)):
        with pytest.raises(ValueError):
            Round3Strategy(*args)


def test_group_equal_uses_current_liquidity_and_no_return_filter():
    prices = {"A": _rise(.1), "B": _rise(.2), "C": _rise(-.1)}
    amounts = {"A": [30_000_000] * 241, "B": [20_000_000] * 241}
    ctx = _context(prices, amounts=amounts, groups={"A": "one", "B": "one", "C": "two"})
    assert GroupEqual(.8).on_close(ctx).weights == {"A": pytest.approx(.35), "C": pytest.approx(.35)}
    assert GroupEqual(.4).on_close(ctx).weights == {"A": pytest.approx(.2), "C": pytest.approx(.2)}
    assert isinstance(GroupEqual().on_close(_context(prices, week_end=False)), Hold)


def test_sector_rotation_ranks_group_median_instead_of_representative_return():
    prices = {"A": _rise(.5), "B": _rise(-.1), "C": _rise(.15)}
    amounts = {"A": [30_000_000] * 241, "B": [20_000_000] * 241}
    groups = {"A": "one", "B": "one", "C": "two"}
    target = Round3Strategy("sector_rotation", 0).on_close(_context(prices, amounts=amounts, groups=groups))
    assert list(target.weights) == ["C", "A"]


def test_low_volatility_prefers_smoother_eligible_trend():
    calm = _rise(.2)
    noisy = [100 + .2 * day + (1 if day % 2 else -1) for day in range(241)]
    target = Round3Strategy("low_vol", 0).on_close(_context({"noisy": noisy, "calm": calm}))
    assert list(target.weights) == ["calm", "noisy"]


def test_breakout_and_pullback_are_distinct_from_plain_momentum():
    fast = _rise(.3)
    dip = _rise(.2)[:-1] + [_rise(.2)[-2] - 2]
    ctx = _context({"fast": fast, "dip": dip})
    assert list(Round3Strategy("momentum", 0).on_close(ctx).weights)[0] == "fast"
    assert list(Round3Strategy("pullback", 0).on_close(ctx).weights) == ["dip"]
    assert list(Round3Strategy("breakout", 0).on_close(ctx).weights) == ["fast"]
