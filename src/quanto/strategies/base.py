from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import copy
import math
from types import MappingProxyType
from typing import Mapping, Protocol

import pandas as pd

from quanto.config import validate_parameters


def _readonly(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_readonly(item) for item in value)
    return copy.deepcopy(value)


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
        dates = self.history["date"]
        if not pd.api.types.is_datetime64_any_dtype(dates.dtype):
            dates = pd.to_datetime(dates)
        history = self.history.loc[dates <= asof].copy(deep=True)
        object.__setattr__(self, "asof", asof)
        object.__setattr__(self, "history", history)
        object.__setattr__(self, "eligible_symbols", tuple(self.eligible_symbols))
        object.__setattr__(self, "pending_orders", _readonly(self.pending_orders))
        object.__setattr__(self, "risk_state", _readonly(self.risk_state))


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


def _calendar_dates(calendar: pd.Series | pd.DatetimeIndex | list[date]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(calendar)).sort_values().unique()


def is_week_end(calendar: pd.Series | pd.DatetimeIndex | list[date], asof: date | pd.Timestamp) -> bool:
    dates = _calendar_dates(calendar)
    asof = pd.Timestamp(asof)
    index = dates.searchsorted(asof)
    if index >= len(dates) or dates[index] != asof:
        raise ValueError("asof must be a calendar session")
    return bool(index + 1 < len(dates) and dates[index + 1].to_period("W-FRI") != asof.to_period("W-FRI"))


def validate_warmup_window(
    strategy: Strategy,
    observed_dates: pd.Series | pd.DatetimeIndex | list[date],
    start: date | pd.Timestamp,
    end: date | pd.Timestamp,
) -> None:
    """Validate an opportunity using observed price dates, not empty sessions."""
    dates = _calendar_dates(observed_dates)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    for index, asof in enumerate(dates):
        if index >= strategy.warmup_bars and start <= asof <= end:
            return
    raise ValueError("No strategy session remains after warmup")
