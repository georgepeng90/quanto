from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


PRICE_COLUMNS = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]
BENCHMARK_COLUMNS = ["date", "close", "turnover"]
INSTRUMENT_COLUMNS = ["symbol", "lot_size", "settlement_sessions", "listing_date", "group"]


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    is_demo: bool
    price_mode: Literal["raw_cash", "adjusted_research"]
    currency: str = Field(min_length=1)
    calendar_start: date
    calendar_end: date
    data_start: date
    data_end: date
    volume_unit: str = Field(min_length=1)
    amount_unit: str = Field(min_length=1)
    event_coverage: Literal["complete", "partial", "unknown"]
    benchmark_type: Literal["price_index", "total_return", "other"]
    field_semantics: dict[str, str]
    universe_rule: dict
    auxiliary_tables: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_coverage(self) -> DatasetManifest:
        if not self.calendar_start <= self.data_start <= self.data_end <= self.calendar_end:
            raise ValueError("data coverage must be inside calendar coverage")
        if self.price_mode == "raw_cash" and self.event_coverage != "complete":
            raise ValueError("raw_cash requires complete event coverage")
        if self.price_mode == "adjusted_research" and not self.limitations:
            raise ValueError("adjusted_research requires disclosed limitations")
        return self


@dataclass(frozen=True)
class DatasetBundle:
    manifest: DatasetManifest
    prices: pd.DataFrame
    benchmark: pd.DataFrame
    calendar: pd.DataFrame
    instruments: pd.DataFrame
    status: pd.DataFrame
    events: pd.DataFrame
    eligibility: pd.DataFrame
    auxiliary: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def price_mode(self) -> str:
        return self.manifest.price_mode
