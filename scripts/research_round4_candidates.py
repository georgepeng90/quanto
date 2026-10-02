"""Breadth hysteresis and fixed-slot basket volatility research candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
import pandas as pd

from quanto.strategies.base import Hold, StrategyContext, TargetPortfolio
from scripts.research_round3_candidates import _above_mean, _candidates, _return


def families() -> dict[str, list[str]]:
    return {
        "hysteresis": ["h55_45", "h55_40", "h60_45", "h60_40"],
        "volatility": ["vol10", "vol12", "vol15"],
        "combined": ["h55_45_vol10", "h55_45_vol12", "h55_45_vol15"],
    }


def configurations() -> dict[str, dict]:
    specs = {}
    for entry, exit in ((.55, .45), (.55, .4), (.6, .45), (.6, .4)):
        name = f"h{round(entry * 100)}_{round(exit * 100)}"
        specs[name] = {"kind": "breadth_risk", "params": {
            "entry_breadth": entry, "exit_breadth": exit}}
    for target in (.10, .12, .15):
        name = f"vol{round(target * 100)}"
        specs[name] = {"kind": "breadth_risk", "params": {"target_vol": target}}
        specs[f"h55_45_{name}"] = {"kind": "breadth_risk", "params": {
            "entry_breadth": .55, "exit_breadth": .45, "target_vol": target}}
    return specs


def _basket_volatility(ctx: StrategyContext, weights: dict[str, float], window: int) -> float | None:
    dates = pd.DatetimeIndex(pd.to_datetime(ctx.history.date).unique()).sort_values()[-window - 1:]
    if len(dates) != window + 1 or dates[-1] != ctx.asof:
        return None
    basket_returns = np.zeros(window, dtype=float)
    for symbol, weight in weights.items():
        rows = (ctx.history.loc[ctx.history.symbol == symbol, ["date", "close"]]
                .sort_values("date").drop_duplicates("date", keep="last"))
        close = rows.set_index("date").close.reindex(dates).to_numpy(dtype=float)
        if not np.isfinite(close).all() or np.any(close <= 0):
            return None
        basket_returns += weight * (close[1:] / close[:-1] - 1)
    return float(np.std(basket_returns, ddof=0) * math.sqrt(252))


@dataclass
class BreadthRiskStrategy:
    entry_breadth: float = .5
    exit_breadth: float = .5
    target_vol: float | None = None
    vol_window: int = 60
    gross_target: float = .8
    warmup_bars: int = field(init=False)
    _active: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("entry_breadth", "exit_breadth"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (float, int))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"{name} must be finite in [0, 1]")
        if self.exit_breadth > self.entry_breadth:
            raise ValueError("exit_breadth must not exceed entry_breadth")
        if (self.target_vol is not None
                and (isinstance(self.target_vol, bool) or not isinstance(self.target_vol, (float, int))
                     or not math.isfinite(self.target_vol) or self.target_vol <= 0)):
            raise ValueError("target_vol must be finite and positive")
        if type(self.vol_window) is not int or self.vol_window < 2:
            raise ValueError("vol_window must be an integer of at least two")
        if (isinstance(self.gross_target, bool) or not isinstance(self.gross_target, (float, int))
                or not math.isfinite(self.gross_target) or not 0 <= self.gross_target <= .8):
            raise ValueError("gross_target must be finite in [0, 0.8]")
        self.warmup_bars = max(200, self.vol_window if self.target_vol is not None else 200)

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside weekly evaluation schedule")
        reps, _ = _candidates(ctx, self.warmup_bars)
        if not reps:
            self._active = False
            return TargetPortfolio({}, {}, {"qualifying_groups": 0})

        breadth = sum(_above_mean(rows.close.to_numpy(dtype=float), 200)
                      for _, rows, _ in reps.values()) / len(reps)
        if self._active:
            if breadth < self.exit_breadth:
                self._active = False
        elif breadth >= self.entry_breadth:
            self._active = True

        scored = []
        for group, (symbol, rows, _) in reps.items():
            close = rows.close.to_numpy(dtype=float)
            score = _return(close, 120) if _above_mean(close, 200) else 0
            if math.isfinite(score) and score > 0:
                scored.append((symbol, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        budget = self.gross_target if self._active else 0
        weights = {symbol: budget / 5 for symbol, _ in scored[:5] if budget > 0}
        diagnostics = {"qualifying_groups": len(reps), "signalled_groups": len(scored),
                       "breadth": breadth, "budget": budget}
        if weights and self.target_vol is not None:
            volatility = _basket_volatility(ctx, weights, self.vol_window)
            if volatility is None:
                diagnostics["risk_history"] = "unavailable"
                return TargetPortfolio({}, {}, diagnostics)
            diagnostics["basket_volatility"] = volatility
            scale = min(1.0, self.target_vol / volatility) if volatility > 0 else 1.0
            weights = {symbol: weight * scale for symbol, weight in weights.items()}
        return TargetPortfolio(weights, {symbol: "breadth_timing" for symbol in weights}, diagnostics)
