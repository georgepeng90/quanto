"""Research-only guarded ETF rotation candidate; does not change application defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Literal

import pandas as pd

from quanto.strategies.base import Hold, StrategyContext, TargetPortfolio


@dataclass(frozen=True)
class GuardedRotation:
    trend_window: int = 120
    momentum_window: int = 60
    selection: Literal["momentum", "pullback"] = "momentum"
    target_volatility: float = .12
    entry_rank: int = 3
    warmup_bars: int = field(init=False)

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in
               (self.trend_window, self.momentum_window)) or type(self.entry_rank) is not int or not 1 <= self.entry_rank <= 3:
            raise ValueError("Windows must be positive and entry rank must be 1 to 3")
        if self.selection not in ("momentum", "pullback"):
            raise ValueError("selection must be momentum or pullback")
        if not math.isfinite(self.target_volatility) or self.target_volatility <= 0:
            raise ValueError("target_volatility must be finite and positive")
        object.__setattr__(self, "warmup_bars", max(self.trend_window, self.momentum_window, 60))

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside weekly evaluation schedule")

        groups = ctx.risk_state.get("symbol_groups", {})
        ranked: list[tuple[str, float, float]] = []
        for symbol in sorted(set(ctx.eligible_symbols)):
            rows = ctx.history.loc[ctx.history.symbol == symbol].sort_values("date")
            rows = rows.drop_duplicates("date", keep="last")
            if len(rows) <= self.warmup_bars or pd.Timestamp(rows.iloc[-1].date) != ctx.asof:
                continue
            closes = rows.close.astype(float)
            recent = closes.iloc[-self.warmup_bars - 1:]
            if not all(math.isfinite(value) and value > 0 for value in recent):
                continue
            latest = closes.iloc[-1]
            momentum = latest / closes.iloc[-1 - self.momentum_window] - 1
            if latest <= closes.iloc[-self.trend_window:].mean() or momentum <= 0:
                continue
            volatility = float(recent.pct_change().iloc[-60:].std(ddof=0) * math.sqrt(252))
            if not math.isfinite(volatility) or volatility <= 0:
                continue
            score = momentum if self.selection == "momentum" else latest / closes.iloc[-21] - 1
            ranked.append((symbol, score, volatility))

        ranked.sort(key=lambda item: (-item[1], item[0]) if self.selection == "momentum"
                    else (item[1], item[0]))
        selected: list[tuple[str, float]] = []
        selected_groups: set[str] = set()
        for symbol, _, volatility in ranked:
            group = groups.get(symbol, symbol)
            if group in selected_groups:
                continue
            selected.append((symbol, volatility))
            selected_groups.add(group)
            if len(selected) >= self.entry_rank:
                break
        if not selected:
            return TargetPortfolio({}, {}, {"eligible_count": 0})

        inverse = {symbol: 1 / volatility for symbol, volatility in selected}
        inverse_sum = sum(inverse.values())
        fractions = {symbol: value / inverse_sum for symbol, value in inverse.items()}
        # Assumes perfectly correlated assets for a conservative volatility estimate.
        weighted_asset_volatility = sum(fractions[symbol] * volatility for symbol, volatility in selected)
        budget = min(.7, self.target_volatility / weighted_asset_volatility) * len(selected) / 3
        weights = {symbol: min(.3, budget * fractions[symbol]) for symbol, _ in selected}
        return TargetPortfolio(weights, {symbol: self.selection for symbol in weights},
                               {"eligible_count": len(selected), "volatility_upper_bound_estimate":
                                sum(weights[symbol] * volatility for symbol, volatility in selected)})
