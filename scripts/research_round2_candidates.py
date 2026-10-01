"""Research-only cash-aware strategy candidates; application defaults are unchanged."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Literal

import pandas as pd

from quanto.strategies.base import Hold, StrategyContext, TargetPortfolio
from quanto.strategies.trend_rotation import TrendRotation


def _budget(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= .8:
        raise ValueError(f"{name} must be finite and between zero and 0.8")


def _window(value: int, name: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _current_rows(ctx: StrategyContext, symbol: str) -> pd.DataFrame:
    rows = ctx.history.loc[ctx.history["symbol"] == symbol]
    rows = rows.sort_values("date").drop_duplicates("date", keep="last")
    if rows.empty or pd.Timestamp(rows.iloc[-1]["date"]) != ctx.asof:
        return rows.iloc[0:0]
    return rows


def _seasoned(ctx: StrategyContext, trend_window: int, volatility: bool = False) -> dict[str, tuple[float, float | None]]:
    seasoned = {}
    required = max(trend_window, 60 if volatility else 0)
    for symbol in sorted(set(ctx.eligible_symbols)):
        rows = _current_rows(ctx, symbol)
        if len(rows) <= required:
            continue
        closes = rows["close"].astype(float).iloc[-required - 1:]
        if not all(math.isfinite(price) and price > 0 for price in closes):
            continue
        recent = closes.iloc[-trend_window:]
        realized_volatility = None
        if volatility:
            realized_volatility = float(closes.pct_change().iloc[-60:].std(ddof=0) * math.sqrt(252))
            if not math.isfinite(realized_volatility) or realized_volatility <= 0:
                continue
        seasoned[symbol] = (float(closes.iloc[-1] > recent.mean()), realized_volatility)
    return seasoned


@dataclass(frozen=True)
class DiversifiedTrend:
    """Reserve each seasoned symbol's allocation when its trend fails."""

    trend_window: int = 200
    gross_target: float = .8
    weighting: Literal["equal", "inverse_vol"] = "equal"
    warmup_bars: int = field(init=False)

    def __post_init__(self) -> None:
        _window(self.trend_window, "trend_window")
        _budget(self.gross_target, "gross_target")
        if self.weighting not in ("equal", "inverse_vol"):
            raise ValueError("weighting must be equal or inverse_vol")
        object.__setattr__(self, "warmup_bars", max(self.trend_window, 60 if self.weighting == "inverse_vol" else 0))

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside weekly evaluation schedule")
        seasoned = _seasoned(ctx, self.trend_window, self.weighting == "inverse_vol")
        if not seasoned:
            return TargetPortfolio({}, {}, {"seasoned_count": 0})
        if self.weighting == "equal":
            slots = {symbol: self.gross_target / len(seasoned) for symbol in seasoned}
        else:
            inverse = {symbol: 1 / value[1] for symbol, value in seasoned.items()}
            denominator = sum(inverse.values())
            slots = {symbol: self.gross_target * value / denominator for symbol, value in inverse.items()}
        weights = {symbol: min(.3, slots[symbol]) for symbol, (passes, _) in seasoned.items() if passes}
        return TargetPortfolio(weights, {symbol: "above trend" for symbol in weights},
                               {"seasoned_count": len(seasoned)})


@dataclass(frozen=True)
class CoreSatellite:
    """Equal-weight current core plus top-three positive-momentum satellite.

    The satellite re-ranks each week without retain logic: core holdings are
    not treated as satellite holdings.
    """

    core_weight: float = .4
    momentum_window: int = 20
    warmup_bars: int = field(init=False)

    def __post_init__(self) -> None:
        _budget(self.core_weight, "core_weight")
        _window(self.momentum_window, "momentum_window")
        object.__setattr__(self, "warmup_bars", self.momentum_window)

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside weekly evaluation schedule")
        current = {}
        ranked = []
        for symbol in sorted(set(ctx.eligible_symbols)):
            rows = _current_rows(ctx, symbol)
            if rows.empty:
                continue
            latest = float(rows.iloc[-1]["close"])
            if not math.isfinite(latest) or latest <= 0:
                continue
            current[symbol] = latest
            if len(rows) <= self.momentum_window:
                continue
            base = float(rows.iloc[-1 - self.momentum_window]["close"])
            if math.isfinite(base) and base > 0 and latest > base:
                ranked.append((symbol, latest / base - 1))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        core_slot = self.core_weight / len(current) if current else 0
        selected = [symbol for symbol, _ in ranked[:3]]
        satellite_slot = (.8 - self.core_weight) / len(selected) if selected else 0
        weights = {symbol: min(.35, core_slot + (satellite_slot if symbol in selected else 0))
                   for symbol in current}
        return TargetPortfolio(weights, {symbol: "core and satellite" if symbol in selected else "core"
                                         for symbol in current}, {"satellite_ranked": ranked})


@dataclass(frozen=True)
class BreadthRotation:
    """Scale the standard rotation target by seasoned trend breadth."""

    trend_window: int = 200
    bear_budget: float = 0
    warmup_bars: int = field(init=False)

    def __post_init__(self) -> None:
        _window(self.trend_window, "trend_window")
        _budget(self.bear_budget, "bear_budget")
        object.__setattr__(self, "warmup_bars", max(self.trend_window, 20))

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside weekly evaluation schedule")
        seasoned = _seasoned(ctx, self.trend_window)
        if not seasoned:
            return TargetPortfolio({}, {}, {"seasoned_count": 0})
        base = TrendRotation(20, 20, 3, 5, "weekly").on_close(ctx)
        if isinstance(base, Hold):
            return base
        budget = .8 if sum(value[0] for value in seasoned.values()) / len(seasoned) >= .5 else self.bear_budget
        weights = {symbol: min(.35, weight * budget) for symbol, weight in base.weights.items() if weight * budget > 0}
        return TargetPortfolio(weights, {symbol: "breadth rotation" for symbol in weights},
                               {"breadth": sum(value[0] for value in seasoned.values()) / len(seasoned),
                                "budget": budget})


@dataclass
class CashBuyHold:
    """Emit a fixed-exposure equal-weight target once, then hold."""

    gross_target: float = .4
    warmup_bars: int = field(default=0, init=False)
    _emitted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _budget(self.gross_target, "gross_target")

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return not self._emitted

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if self._emitted:
            return Hold("initial target already emitted")
        self._emitted = True
        symbols = sorted(set(ctx.eligible_symbols))
        weight = self.gross_target / len(symbols) if symbols else 0
        return TargetPortfolio({symbol: weight for symbol in symbols},
                               {symbol: "initial equal weight" for symbol in symbols}, {})
