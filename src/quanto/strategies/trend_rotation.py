from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Literal

import pandas as pd

from .base import Hold, StrategyContext, TargetPortfolio


@dataclass(frozen=True)
class TrendRotation:
    trend_window: int = 20
    momentum_window: int = 20
    entry_rank: int = 3
    retain_rank: int = 5
    evaluation: Literal["daily", "weekly"] = "weekly"
    warmup_bars: int = field(init=False)

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (self.trend_window, self.momentum_window, self.entry_rank, self.retain_rank)):
            raise ValueError("Trend windows and ranks must be positive integers")
        if self.retain_rank < self.entry_rank:
            raise ValueError("retain_rank must be at least entry_rank")
        if self.evaluation not in ("daily", "weekly"):
            raise ValueError("evaluation must be daily or weekly")
        object.__setattr__(self, "warmup_bars", max(self.trend_window, self.momentum_window))

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return self.evaluation == "daily" or ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside evaluation schedule")
        available_dates = pd.DatetimeIndex(pd.to_datetime(ctx.history["date"].unique())).sort_values()
        if len(available_dates) <= self.warmup_bars:
            return Hold("insufficient warmup history")

        ranked: list[tuple[str, float]] = []
        for symbol in sorted(set(ctx.eligible_symbols)):
            rows = ctx.history.loc[ctx.history["symbol"] == symbol].sort_values("date")
            rows = rows.drop_duplicates("date", keep="last")
            if rows.empty or pd.Timestamp(rows.iloc[-1]["date"]) != ctx.asof or len(rows) <= self.warmup_bars:
                continue
            closes = rows["close"].astype(float)
            latest = closes.iloc[-1]
            trend_base = closes.iloc[-1 - self.trend_window]
            momentum_base = closes.iloc[-1 - self.momentum_window]
            if not all(math.isfinite(value) and value > 0 for value in (latest, trend_base, momentum_base)):
                continue
            if latest / trend_base <= 1:
                continue
            ranked.append((symbol, latest / momentum_base - 1))

        ranked.sort(key=lambda item: (-item[1], item[0]))
        ranks = {symbol: index for index, (symbol, _) in enumerate(ranked, start=1)}
        held = {symbol for symbol, shares in ctx.portfolio.shares.items() if shares > 0}
        selected = [symbol for symbol in sorted(held) if ranks.get(symbol, self.retain_rank + 1) <= self.retain_rank]
        if len(selected) > self.entry_rank:
            selected = sorted(selected, key=lambda symbol: (ranks[symbol], symbol))[:self.entry_rank]
        for symbol, _ in ranked[:self.entry_rank]:
            if len(selected) >= self.entry_rank:
                break
            if symbol not in selected:
                selected.append(symbol)
        weight = 1 / len(selected) if selected else 0
        return TargetPortfolio({symbol: weight for symbol in selected}, {symbol: ("retained" if symbol in held else "momentum entry") for symbol in selected}, {"ranked": ranked})
