from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


BAR_COLUMNS = ("instrument_id", "trading_date", "bar_start", "bar_end", "open", "high", "low", "close", "volume", "amount")
BAR_KEY = ("instrument_id", "bar_start", "bar_end")
BAR_ORDER = ("bar_end", "instrument_id", "bar_start")
Hash = str


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def content_key(value: object) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentId(Contract):
    exchange: str = Field(pattern=r"^[A-Z0-9_]+$")
    instrument_type: Literal["etf", "index", "stock"]
    code: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.instrument_type}:{self.code}"


class PriceBasis(Contract):
    mode: Literal["raw", "verified_adjusted", "vendor_research"]
    basis_id: str | None = None

    @model_validator(mode="after")
    def check_basis(self):
        if self.mode == "verified_adjusted" and not self.basis_id:
            raise ValueError("verified adjustment requires factor/anchor identity")
        return self


class DataRequest(Contract):
    kind: str
    instruments: tuple[InstrumentId, ...] = ()
    start: datetime
    end: datetime
    columns: tuple[str, ...]
    frequency: Literal["1d", "1m"] | None = None
    price_basis: PriceBasis | None = None
    currency: str = "CNY"

    @field_validator("start", "end")
    @classmethod
    def timestamps(cls, value):
        return utc(value)

    @field_validator("kind")
    @classmethod
    def kinds(cls, value):
        if value not in {"bars", "calendar", "instruments", "trading_status", "corporate_actions"} and not re.fullmatch(r"auxiliary:[A-Za-z0-9_-]+", value):
            raise ValueError("unsupported table kind")
        return value

    @model_validator(mode="after")
    def check_request(self):
        if self.start >= self.end or not self.columns or len(set(self.columns)) != len(self.columns):
            raise ValueError("nonempty interval and distinct fields required")
        if self.kind == "bars" and (not self.instruments or not self.frequency or self.price_basis is None):
            raise ValueError("bars require instruments, frequency and price basis")
        if self.kind != "bars" and (self.frequency is not None or self.price_basis is not None):
            raise ValueError("reference tables have no bar frequency or price basis")
        if len(set(self.instruments)) != len(self.instruments):
            raise ValueError("duplicate instrument")
        return self

    @property
    def series_key(self) -> str:
        data = self.model_dump(mode="json", exclude={"start", "end", "columns"})
        data["instruments"] = sorted(data["instruments"], key=lambda item: (item["exchange"], item["instrument_type"], item["code"]))
        return content_key(data)


class DataQuery(DataRequest):
    @model_validator(mode="after")
    def resolved_basis(self):
        if self.price_basis and self.price_basis.mode == "vendor_research" and not self.price_basis.basis_id:
            raise ValueError("query must select a resolved price basis")
        return self


class TableQuery(Contract):
    instruments: tuple[InstrumentId, ...] = ()
    start: datetime
    end: datetime
    columns: tuple[str, ...]

    @field_validator("start", "end")
    @classmethod
    def timestamps(cls, value):
        return utc(value)

    @model_validator(mode="after")
    def interval(self):
        if self.start >= self.end or not self.columns:
            raise ValueError("nonempty query required")
        return self


class QualityPolicy(Contract):
    required_evidence: tuple[str, ...] = ()
    accepted_limitations: tuple[str, ...] = ()


class DataRequirement(Contract):
    requests: tuple[DataRequest, ...] = Field(min_length=1)
    quality: QualityPolicy = Field(default_factory=QualityPolicy)

    @property
    def key(self) -> str:
        data = self.model_dump(mode="json")
        for request in data["requests"]:
            request["columns"] = sorted(request["columns"])
            request["instruments"] = sorted(request["instruments"], key=lambda item: (item["exchange"], item["instrument_type"], item["code"]))
        data["requests"] = sorted(data["requests"], key=content_key)
        data["quality"] = {key: sorted(values) for key, values in data["quality"].items()}
        return content_key(data)


class SourceRecord(Contract):
    provider: str
    interface: str
    adapter_version: str = "1"
    upstream: str
    parameters: dict = Field(default_factory=dict)
    downloaded_at: datetime
    actual_mode: str
    evidence_hash: str = ""
    units: dict[str, str] = Field(default_factory=dict)
    verified: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    available_at: datetime | None = None
    availability_assumption: str | None = None

    @field_validator("downloaded_at", "available_at")
    @classmethod
    def timestamps(cls, value):
        return utc(value) if value is not None else None


@dataclass(frozen=True)
class NormalizedChunk:
    request: DataRequest
    frame: pd.DataFrame
    source: SourceRecord
    evidence: bytes = b""


class FragmentRef(Contract):
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: DataRequest
    source: SourceRecord
    source_evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0)
    quality: dict = Field(default_factory=dict)

    @property
    def series_id(self) -> str:
        return self.request.series_key


class CoverageEntry(Contract):
    request: DataRequest
    missing: tuple[datetime, ...] = ()
    missing_fields: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def complete(self) -> bool:
        return not (self.missing or self.missing_fields or self.missing_evidence or self.reason)


class CoverageReport(Contract):
    entries: tuple[CoverageEntry, ...] = ()
    limitations: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.entries) and all(entry.complete for entry in self.entries)


class SnapshotManifest(Contract):
    contract_version: Literal[1] = 1
    requirement: DataRequirement
    fragments: tuple[FragmentRef, ...]
    coverage: CoverageReport
    quality: dict = Field(default_factory=dict)


class SnapshotRef(Contract):
    id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    quality: dict = Field(default_factory=dict)


class PrefetchTaskRef(Contract):
    id: str


class PrefetchStatus(Contract):
    state: Literal["queued", "running", "ready", "incomplete", "failed", "cancelled"]
    progress: float = 0
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    snapshot: SnapshotRef | None = None
    reason: str | None = None
    limitations: tuple[str, ...] = ()


class MarketDataError(Exception):
    def __init__(self, code: str, message: str, coverage: CoverageReport | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.coverage = code, coverage
