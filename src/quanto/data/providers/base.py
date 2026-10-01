from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Protocol

import pandas as pd

from quanto.data.contracts import BAR_COLUMNS, DataRequest, MarketDataError, NormalizedChunk


@dataclass(frozen=True)
class FetchContext:
    deadline: float
    cancelled: Callable[[], bool]

    def remaining(self) -> float:
        if self.cancelled():
            raise MarketDataError("CANCELLED", "source request cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MarketDataError("FAILED", "source deadline exceeded")
        return remaining


@dataclass(frozen=True)
class ProviderCapabilities:
    kinds: tuple[str, ...]
    frequencies: tuple[str, ...]
    fields: dict[str, tuple[str, ...]]
    modes: tuple[str, ...] = ("vendor_research",)
    exchanges: tuple[str, ...] = ()
    currencies: tuple[str, ...] = ()

    def supports(self, request: DataRequest) -> bool:
        if request.kind not in self.kinds or (self.currencies and request.currency not in self.currencies) or (
            self.exchanges and any(item.exchange not in self.exchanges for item in request.instruments)):
            return False
        if request.kind != "bars":
            return True
        return request.frequency in self.frequencies and request.price_basis.mode in self.modes and all(
            instrument.instrument_type in self.fields and set(request.columns).issubset(set(self.fields[instrument.instrument_type]).union(BAR_COLUMNS[:4]))
            for instrument in request.instruments)


class MarketProvider(Protocol):
    name: str
    def capabilities(self) -> ProviderCapabilities: ...
    def fetch(self, request: DataRequest, context: FetchContext) -> NormalizedChunk: ...


def local_date_bounds(request: DataRequest):
    return pd.Timestamp(request.start).tz_convert("Asia/Shanghai").date(), (
        pd.Timestamp(request.end) - pd.Timedelta(nanoseconds=1)).tz_convert("Asia/Shanghai").date()


def daily_bars(frame: pd.DataFrame, instrument, request: DataRequest) -> pd.DataFrame:
    frame = frame.copy()
    days = pd.to_datetime(frame.pop("date")).dt.normalize()
    frame["trading_date"] = days.dt.date
    local = days.dt.tz_localize("Asia/Shanghai")
    frame["bar_start"] = (local + pd.Timedelta(hours=9, minutes=30)).dt.tz_convert("UTC")
    frame["bar_end"] = (local + pd.Timedelta(hours=15)).dt.tz_convert("UTC")
    frame["instrument_id"] = instrument.key
    frame = frame[(frame.bar_start >= request.start) & (frame.bar_end <= request.end)]
    return frame.reset_index(drop=True)
