"""Frozen research candidates for the 305-ETF equity-only cache.

Every family evaluates on the weekly close and targets next-session execution.
Only currently eligible ETFs with a current close, at least 120 own observations,
and at least CNY 10m mean amount over their last 20 observed sessions enter.
One ETF per catalog group is chosen by trailing amount, then symbol as a tie.
The five investment slots are fixed: missing signals remain cash. No external
market series, end-of-study size/rank, or future history enters a decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
import pandas as pd

from quanto.strategies.base import Hold, StrategyContext, TargetPortfolio


FAMILY_RULES = {
    "momentum": ("Rank positive 60-session returns", "Rank positive 120-session returns"),
    "dual_momentum": ("Rank mean of positive 60/120 returns", "Rank mean of positive 120/200 returns"),
    "ma_trend": ("Rank positive close/120-session mean minus one", "Rank positive close/200-session mean minus one"),
    "breakout": ("New 60-session close high; retain held above 20-session mean",
                 "New 120-session close high; retain held above 20-session mean"),
    "low_vol": ("Above 120-session mean with positive 60-session return; rank least 60-session volatility",
                "Above 200-session mean with positive 60-session return; rank least 120-session volatility"),
    "regression": ("Above 120-session mean; rank positive 60-session log slope times R-squared",
                   "Above 200-session mean; rank positive 120-session log slope times R-squared"),
    "pullback": ("Above 120-session mean with positive 60-session return; rank negative 5-session return",
                 "Above 200-session mean with positive 60-session return; rank negative 20-session return"),
    "sector_rotation": ("Rank positive median 60-session return within each catalog group",
                        "Rank positive median 120-session return within each catalog group"),
    "breadth_timing": ("Rank positive 60-session momentum above 120-session mean; halve budget below 50% group breadth",
                       "Rank positive 120-session momentum above 200-session mean; zero budget below 50% group breadth"),
    "correlation": ("Rank positive 60-session momentum above 120-session mean; greedy 60-session return correlation <= 0.8",
                    "Rank positive 120-session momentum above 200-session mean; greedy 120-session return correlation <= 0.7"),
}

_WINDOWS = {
    "momentum": (120, 120), "dual_momentum": (120, 200),
    "ma_trend": (120, 200), "breakout": (120, 120),
    "low_vol": (120, 200), "regression": (120, 200),
    "pullback": (120, 200), "sector_rotation": (120, 120),
    "breadth_timing": (120, 200), "correlation": (120, 200),
}


def descriptions() -> dict[str, list[str]]:
    return {family: list(rules) for family, rules in FAMILY_RULES.items()}


def configurations() -> dict[str, dict]:
    return {f"{family}_v{variant}": {"kind": "round3", "params": {"family": family, "variant": variant}}
            for family in FAMILY_RULES for variant in (0, 1)}


def _candidates(ctx: StrategyContext, own_window: int):
    """Return group representatives and the eligible group members for group signals."""
    if "amount" not in ctx.history or "close" not in ctx.history or "symbol" not in ctx.history:
        return {}, {}
    eligible = set(ctx.eligible_symbols)
    groups = ctx.risk_state.get("symbol_groups", {})
    members = {}
    for symbol, rows in ctx.history.loc[ctx.history.symbol.isin(eligible)].groupby("symbol", sort=True):
        rows = rows.sort_values("date").drop_duplicates("date", keep="last")
        if len(rows) <= own_window or pd.Timestamp(rows.iloc[-1].date) != ctx.asof:
            continue
        rows = rows.iloc[-own_window - 1:]
        close = rows.close.to_numpy(dtype=float)
        amount = rows.amount.to_numpy(dtype=float)[-20:]
        if (len(amount) < 20 or not np.isfinite(close).all() or np.any(close <= 0)
                or not np.isfinite(amount).all() or np.any(amount < 0)):
            continue
        liquidity = float(amount.mean())
        if liquidity < 10_000_000:
            continue
        group = str(groups.get(symbol, symbol))
        members.setdefault(group, []).append((symbol, rows, liquidity))
    reps = {group: sorted(items, key=lambda item: (-item[2], item[0]))[0]
            for group, items in members.items()}
    return reps, members


def _return(close: np.ndarray, window: int) -> float:
    return float(close[-1] / close[-window - 1] - 1)


def _above_mean(close: np.ndarray, window: int) -> bool:
    return bool(close[-1] > close[-window:].mean())


def _volatility(close: np.ndarray, window: int) -> float:
    returns = np.diff(close[-window - 1:]) / close[-window - 1:-1]
    return float(np.std(returns) * math.sqrt(252))


def _regression(close: np.ndarray, window: int) -> float:
    y = np.log(close[-window:])
    x = np.arange(window, dtype=float)
    slope = float(np.dot(x - x.mean(), y - y.mean()) / np.dot(x - x.mean(), x - x.mean()))
    fit = y.mean() + slope * (x - x.mean())
    variance = float(np.dot(y - y.mean(), y - y.mean()))
    r_squared = 1 - float(np.dot(y - fit, y - fit)) / variance if variance > 0 else 0
    return slope * window * max(0, r_squared)


def _correlation(left: pd.DataFrame, right: pd.DataFrame, window: int) -> float:
    a = left[["date", "close"]].iloc[-window - 1:].copy()
    b = right[["date", "close"]].iloc[-window - 1:].copy()
    a["return_a"] = a.close.pct_change()
    b["return_b"] = b.close.pct_change()
    joined = a[["date", "return_a"]].merge(b[["date", "return_b"]], on="date").dropna()
    if len(joined) < window or joined.return_a.std(ddof=0) <= 0 or joined.return_b.std(ddof=0) <= 0:
        return 1.0
    value = float(joined.return_a.corr(joined.return_b))
    return value if math.isfinite(value) else 1.0


def _signal(family: str, variant: int, close: np.ndarray, held: bool) -> float | None:
    momentum = 60 if variant == 0 else 120
    trend = 120 if variant == 0 else 200
    if family == "momentum":
        value = _return(close, momentum)
    elif family == "dual_momentum":
        short, long = (60, 120) if variant == 0 else (120, 200)
        first, second = _return(close, short), _return(close, long)
        value = (first + second) / 2 if first > 0 and second > 0 else 0
    elif family == "ma_trend":
        value = float(close[-1] / close[-trend:].mean() - 1)
    elif family == "breakout":
        high = float(close[-(60 if variant == 0 else 120) - 1:-1].max())
        breakout = float(close[-1] / high - 1)
        value = breakout if breakout > 0 else (1e-12 if held and _above_mean(close, 20) else 0)
    elif family == "low_vol":
        if not _above_mean(close, trend) or _return(close, 60) <= 0:
            return None
        volatility = _volatility(close, 60 if variant == 0 else 120)
        value = 1 / volatility if volatility > 0 else 0
    elif family == "regression":
        value = _regression(close, 60 if variant == 0 else 120) if _above_mean(close, trend) else 0
    elif family == "pullback":
        dip = _return(close, 5 if variant == 0 else 20)
        value = -dip if _above_mean(close, trend) and _return(close, 60) > 0 and dip < 0 else 0
    else:
        return None
    return value if math.isfinite(value) and value > 0 else None


@dataclass(frozen=True)
class Round3Strategy:
    family: str
    variant: int
    gross_target: float = .8
    warmup_bars: int = field(init=False)

    def __post_init__(self):
        if self.family not in FAMILY_RULES or type(self.variant) is not int or self.variant not in (0, 1):
            raise ValueError("unknown family or variant")
        if (isinstance(self.gross_target, bool) or not isinstance(self.gross_target, (float, int))
                or not math.isfinite(self.gross_target) or not 0 <= self.gross_target <= .8):
            raise ValueError("gross_target must be finite in [0, 0.8]")
        object.__setattr__(self, "warmup_bars", _WINDOWS[self.family][self.variant])

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside weekly evaluation schedule")
        reps, members = _candidates(ctx, self.warmup_bars)
        if not reps:
            return TargetPortfolio({}, {}, {"qualifying_groups": 0})
        scored = []
        for group, (symbol, rows, _) in reps.items():
            close = rows.close.to_numpy(dtype=float)
            if self.family == "sector_rotation":
                window = 60 if self.variant == 0 else 120
                score = float(np.median([_return(item[1].close.to_numpy(dtype=float), window)
                                         for item in members[group]]))
            elif self.family in ("breadth_timing", "correlation"):
                window = 60 if self.variant == 0 else 120
                trend = 120 if self.variant == 0 else 200
                score = _return(close, window) if _above_mean(close, trend) else 0
            else:
                score = _signal(self.family, self.variant, close, ctx.portfolio.shares.get(symbol, 0) > 0)
            if score is not None and math.isfinite(score) and score > 0:
                scored.append((symbol, score, group, rows))
        scored.sort(key=lambda item: (-item[1], item[0]))
        budget = self.gross_target
        if self.family == "breadth_timing":
            trend = 120 if self.variant == 0 else 200
            breadth = sum(_above_mean(rows.close.to_numpy(dtype=float), trend)
                          for _, rows, _ in reps.values()) / len(reps)
            if breadth < .5:
                budget *= .5 if self.variant == 0 else 0
        selected = []
        if self.family == "correlation":
            window, limit = (60, .8) if self.variant == 0 else (120, .7)
            for item in scored:
                if all(_correlation(item[3], chosen[3], window) <= limit for chosen in selected):
                    selected.append(item)
                if len(selected) == 5:
                    break
        else:
            selected = scored[:5]
        weights = {symbol: min(.35, budget / 5) for symbol, _, _, _ in selected if budget > 0}
        return TargetPortfolio(weights, {symbol: self.family for symbol in weights},
                               {"qualifying_groups": len(reps), "signalled_groups": len(scored), "budget": budget})


@dataclass(frozen=True)
class GroupEqual:
    """Weekly equal weight of one historically liquid ETF per current catalog group."""

    gross_target: float = .8
    warmup_bars: int = field(default=120, init=False)

    def __post_init__(self):
        if (isinstance(self.gross_target, bool) or not isinstance(self.gross_target, (float, int))
                or not math.isfinite(self.gross_target) or not 0 <= self.gross_target <= .8):
            raise ValueError("gross_target must be finite in [0, 0.8]")

    def should_evaluate(self, ctx: StrategyContext) -> bool:
        return ctx.is_week_end

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio:
        if not self.should_evaluate(ctx):
            return Hold("outside weekly evaluation schedule")
        reps, _ = _candidates(ctx, self.warmup_bars)
        allocation = min(.35, self.gross_target / len(reps)) if reps else 0
        weights = {symbol: allocation for symbol in sorted(item[0] for item in reps.values()) if allocation > 0}
        return TargetPortfolio(weights, {symbol: "group equal" for symbol in weights},
                               {"qualifying_groups": len(reps)})
