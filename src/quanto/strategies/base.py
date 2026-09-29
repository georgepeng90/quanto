from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import copy
import math
from types import MappingProxyType
from typing import Mapping, Protocol

import pandas as pd

from quanto.config import validate_parameters


@dataclass(frozen=True)
class PortfolioView:
    cash: float
    receivables: float
    shares: Mapping[str, int]
    sellable_shares: Mapping[str, int]
    market_values: Mapping[str, float]
    equity: float
    stale_value: float

    def __post_init__(self) -> None:
        for name in ("shares", "sellable_shares", "market_values"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class StrategyContext:
    asof: pd.Timestamp
    history: pd.DataFrame
    eligible_symbols: tuple[str, ...]
    portfolio: PortfolioView
    pending_orders: tuple[Mapping, ...]
    risk_state: Mapping
    is_week_end: bool

    def __post_init__(self) -> None:
        asof = pd.Timestamp(self.asof)
        if "date" not in self.history:
            raise ValueError("Strategy history requires a date column")
        history = self.history.loc[pd.to_datetime(self.history["date"]) <= asof].copy(deep=True)
        object.__setattr__(self, "asof", asof)
        object.__setattr__(self, "history", history)
        object.__setattr__(self, "eligible_symbols", tuple(self.eligible_symbols))
        object.__setattr__(self, "pending_orders", tuple(MappingProxyType(copy.deepcopy(dict(order))) for order in self.pending_orders))
        object.__setattr__(self, "risk_state", MappingProxyType(copy.deepcopy(dict(self.risk_state))))


@dataclass(frozen=True)
class Hold:
    reason: str


@dataclass(frozen=True)
class TargetPortfolio:
    weights: dict[str, float]
    reasons: dict[str, str]
    diagnostics: dict


class Strategy(Protocol):
    warmup_bars: int

    def should_evaluate(self, ctx: StrategyContext) -> bool: ...

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio: ...


def validate_intent(intent: Hold | TargetPortfolio, known_symbols: set[str]) -> None:
    if isinstance(intent, Hold):
        return
    if not isinstance(intent, TargetPortfolio):
        raise ValueError("Strategy must return Hold or TargetPortfolio")
    if not isinstance(intent.weights, dict):
        raise ValueError("Target weights must be a mapping with unique symbols")
    if not set(intent.weights).issubset(known_symbols):
        raise ValueError("Target contains unknown symbols")
    if any(not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(weight) or weight < 0 for weight in intent.weights.values()):
        raise ValueError("Target weights must be finite and nonnegative")
    if sum(intent.weights.values()) > 1 + 1e-12:
        raise ValueError("Target weights exceed one")


def validate_evaluation_window(
    strategy: Strategy,
    calendar: pd.Series | pd.DatetimeIndex | list[date],
    start: date | pd.Timestamp,
    end: date | pd.Timestamp,
) -> None:
    dates = pd.DatetimeIndex(pd.to_datetime(calendar)).sort_values().unique()
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    for index, asof in enumerate(dates):
        if index < strategy.warmup_bars or not start <= asof <= end:
            continue
        is_week_end = index == len(dates) - 1 or dates[index + 1].to_period("W-FRI") != asof.to_period("W-FRI")
        if getattr(strategy, "evaluation", "daily") == "daily" or is_week_end:
            return
    raise ValueError("No strategy evaluation session remains after warmup")
