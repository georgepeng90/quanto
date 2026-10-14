"""Cutoff-bound market views. Only the engine receives the unfiltered dataset."""
from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import model_validator

from quanto.data.contracts import utc
from quanto.framework import AssetBinding, FrozenContract, Name, TableSemanticBinding, canonical_hash, thaw_json

if TYPE_CHECKING:
    from quanto.factors.features import FeatureFrame


SHANGHAI = ZoneInfo("Asia/Shanghai")
_ENGINE_VIEW_TOKEN = object()
_BUILTIN_KEYS = {"prices": ("symbol", "date"), "benchmark": ("date",), "calendar": ("date",),
                 "instruments": ("symbol",), "status": ("symbol", "date"), "events": ("symbol", "event_type", "ex_date")}
CORE_TABLES = tuple(_BUILTIN_KEYS)
STOCK_TABLE_VERSIONS = (("stock-fee-rules", "1"), ("stock-open-status", "1"), ("stock-sources", "1"))
_BUILTIN_MODES = {"prices": "session_close", "benchmark": "session_close", "calendar": "declared_calendar",
                  "instruments": "declared_static_reference", "status": "session_close", "events": "execution_event"}


def session_cutoff(day: date) -> datetime:
    return datetime.combine(day, time(15), SHANGHAI).astimezone(timezone.utc)


class TemporalRules(FrozenContract):
    availability_rule: Literal["session_close", "timestamp", "next_session_date", "declared_calendar", "declared_static_reference", "execution_event"]
    business_keys: tuple[Name, ...]
    symbol_column: Name | None = None
    date_column: Name | None = None
    available_column: Name | None = None
    revision_policy: Literal["none", "sequence", "available_at"] = "none"
    revision_id_column: Name | None = None
    revision_order_column: Name | None = None
    effective_from_column: Name | None = None
    effective_to_column: Name | None = None
    effective_entity_keys: tuple[Name, ...] = ()
    effective_date_only: bool = False
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    price_mode: Literal["raw_cash", "adjusted_research"] | None = None
    assumption: str = ""

    @model_validator(mode="after")
    def explicit_rules(self):
        if not self.business_keys or len(set(self.business_keys)) != len(self.business_keys):
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: distinct business keys required")
        if self.availability_rule in {"timestamp", "next_session_date"} and self.available_column is None:
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: availability column required")
        if self.availability_rule in {"session_close", "declared_calendar"} and self.date_column is None:
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: session date column required")
        if self.revision_policy != "none" and self.revision_id_column is None:
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: revision identity required")
        if self.revision_policy == "sequence" and self.revision_order_column is None:
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: revision ordering evidence required")
        if self.effective_to_column and not self.effective_from_column:
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: effective range requires a start")
        if self.effective_entity_keys and (not self.effective_from_column or not set(self.effective_entity_keys).issubset(self.business_keys)):
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: effective entity keys must identify the interval facts")
        return self


class TableSemantics(FrozenContract):
    binding: TableSemanticBinding
    rules: TemporalRules
    reader_version: Literal["pit-reader-v1"] = "pit-reader-v1"

    @staticmethod
    def declared_rules(binding):
        rules = thaw_json(binding.temporal_rules)
        builtin = binding.table in CORE_TABLES and (binding.semantic_id, binding.version) == (f"daily-{binding.table}", "1")
        if builtin:
            defaults = {"availability_rule": _BUILTIN_MODES[binding.table], "business_keys": _BUILTIN_KEYS[binding.table],
                        "symbol_column": "symbol" if binding.table in {"prices", "instruments", "status", "events"} else None}
            if binding.table in {"prices", "benchmark", "status", "calendar"}:
                defaults["date_column"] = "date"
            for key, expected in defaults.items():
                if key in rules and thaw_json(rules[key]) != thaw_json(expected):
                    raise ValueError("BINDING_MISMATCH: built-in table rule declaration differs")
            if set(rules) - {*defaults, "timezone", "price_mode", "assumption", "date_column"}:
                raise ValueError("BINDING_MISMATCH: unregistered extension to built-in time semantics")
            rules = {**defaults, **rules}
        return TemporalRules.model_validate(rules)

    @model_validator(mode="after")
    def matches_declaration(self):
        if self.rules != self.declared_rules(self.binding):
            raise ValueError("BINDING_MISMATCH: reader rules differ from frozen declaration")
        return self

    @classmethod
    def from_binding(cls, binding: TableSemanticBinding, *, supported_versions: Collection[tuple[str, str]] = ()) -> TableSemantics:
        # Non-builtins are enabled only by a trusted, explicitly registered definition.
        builtin = binding.table in CORE_TABLES and (binding.semantic_id, binding.version) == (f"daily-{binding.table}", "1")
        if not builtin and (binding.semantic_id, binding.version) not in (*supported_versions, *STOCK_TABLE_VERSIONS):
            raise ValueError(f"DEFINITION_UNAVAILABLE: no reader for {binding.semantic_id}@{binding.version}")
        return cls(binding=binding, rules=cls.declared_rules(binding))


def _date_only(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, (datetime, pd.Timestamp)):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    return None


def _timestamp(value, *, allow_date=False) -> pd.Timestamp:
    day = _date_only(value)
    if day is not None:
        if allow_date:
            return pd.Timestamp(datetime.combine(day, time.min, SHANGHAI)).tz_convert("UTC")
        raise ValueError("TEMPORAL_EVIDENCE_MISSING: date-only disclosure needs an explicit conservative rule")
    try:
        stamp = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("TEMPORAL_EVIDENCE_MISSING: invalid timestamp") from exc
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("TEMPORAL_EVIDENCE_MISSING: timezone-aware timestamp required")
    return stamp.tz_convert("UTC")


def _copy_frame(frame):
    copied = frame.copy(deep=True)
    for column in copied.columns:
        if copied[column].dtype == object:
            copied[column] = copied[column].map(deepcopy)
    return copied


def _visible(frame, semantics, cutoff, sessions, dataset_id):
    rules = semantics.rules
    required = {*rules.business_keys, *semantics.binding.schema_definition}
    required.update(value for value in (rules.symbol_column, rules.date_column, rules.available_column,
                                        rules.revision_id_column, rules.revision_order_column,
                                        rules.effective_from_column, rules.effective_to_column) if value)
    if not required.issubset(frame.columns):
        raise ValueError(f"TEMPORAL_EVIDENCE_MISSING: missing declared fields in {semantics.binding.table}")
    frame = _copy_frame(frame.loc[:, sorted(required)])
    known = []
    for _, row in frame.iterrows():
        if rules.availability_rule in {"session_close", "declared_calendar"}:
            day = pd.Timestamp(row[rules.date_column]).date()
            if day not in sessions:
                raise ValueError("TEMPORAL_EVIDENCE_MISSING: observation date absent from trading calendar")
            stamp = session_cutoff(day)
        elif rules.availability_rule == "next_session_date":
            day = _date_only(row[rules.available_column])
            if day is None:
                raise ValueError("TEMPORAL_EVIDENCE_MISSING: conservative rule requires a disclosure date")
            following = next((session for session in sessions if session > day), None)
            if following is None:
                # A disclosure after this cutoff is certainly unavailable, even without a future calendar.
                if day >= cutoff.astimezone(SHANGHAI).date():
                    known.append(pd.NaT)
                    continue
                raise ValueError("TEMPORAL_EVIDENCE_MISSING: following trading session unknown")
            stamp = session_cutoff(following)
        else:
            stamp = _timestamp(row[rules.available_column])
        known.append(stamp)
    frame["source_available_at"] = pd.to_datetime(known, utc=True)
    frame = frame.loc[frame.source_available_at <= cutoff].copy()
    if frame.empty:
        frame["source_ref"] = pd.Series(dtype="str")
        return frame
    keys = list(rules.business_keys)
    if frame[keys].isna().any().any():
        raise ValueError("TEMPORAL_EVIDENCE_MISSING: missing business identity")
    if rules.revision_policy == "none":
        if frame.duplicated(keys).any():
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: duplicate facts without revision ordering")
    else:
        revision = rules.revision_id_column
        if frame[revision].isna().any() or frame.duplicated([*keys, revision]).any():
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: duplicate or missing revision identity")
        order = "source_available_at"
        if rules.revision_policy == "sequence":
            order = rules.revision_order_column
            values = pd.to_numeric(frame[order], errors="coerce")
            if values.isna().any() or (values % 1 != 0).any():
                raise ValueError("TEMPORAL_EVIDENCE_MISSING: revision sequence must be an integer")
            frame[order] = values
        if frame.duplicated([*keys, order]).any():
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: ambiguous revision order")
        frame = frame.sort_values(order).groupby(keys, sort=False, dropna=False).tail(1)
    if rules.effective_from_column:
        starts = frame[rules.effective_from_column].map(lambda value: _timestamp(value, allow_date=rules.effective_date_only))
        if rules.effective_to_column:
            ends = pd.to_datetime(frame[rules.effective_to_column].map(lambda value: pd.NaT if pd.isna(value) else _timestamp(value, allow_date=rules.effective_date_only)), utc=True)
            if ((ends.notna()) & (ends <= starts)).any():
                raise ValueError("TEMPORAL_EVIDENCE_MISSING: invalid effective interval")
            active = (starts <= cutoff) & (ends.isna() | (cutoff < ends))
        else:
            active = starts <= cutoff
        frame = frame.loc[active].copy()
        if rules.effective_entity_keys and frame.duplicated(list(rules.effective_entity_keys)).any():
            raise ValueError("TEMPORAL_EVIDENCE_MISSING: overlapping effective intervals")
    identity_fields = [*keys, "source_available_at"]
    if rules.revision_id_column:
        identity_fields.append(rules.revision_id_column)
    frame["source_ref"] = [f"{dataset_id}:{semantics.binding.table}:" + canonical_hash({name: str(row[name]) for name in identity_fields}) for _, row in frame.iterrows()]
    return frame.sort_values(keys).reset_index(drop=True)


@dataclass(frozen=True, init=False)
class PointInTimeView:
    asof: date
    signal_cutoff: datetime
    ordered_symbols: tuple[str, ...]
    dataset_content_hash: str
    binding_hash: str
    price_mode: Literal["raw_cash", "adjusted_research"] | None
    _tables: Mapping[str, pd.DataFrame]
    _semantics: Mapping[str, TableSemantics]
    _realized_splits: pd.DataFrame
    _split_semantics_valid: bool
    _feature_frames: dict[str, FeatureFrame] = field(compare=False, repr=False)

    def __init__(self, token, *, cutoff, symbols, binding, tables, semantics, realized_splits, split_semantics_valid):
        if token is not _ENGINE_VIEW_TOKEN:
            raise TypeError("Only the engine can bind a market view")
        for name, value in dict(asof=cutoff.astimezone(SHANGHAI).date(), signal_cutoff=cutoff, ordered_symbols=tuple(symbols),
                                dataset_content_hash=binding.dataset_content_hash, binding_hash=binding.binding_hash,
                                price_mode=semantics["prices"].rules.price_mode,
                                _tables=MappingProxyType(tables), _semantics=MappingProxyType(semantics),
                                _realized_splits=realized_splits, _split_semantics_valid=split_semantics_valid,
                                _feature_frames={}).items():
            object.__setattr__(self, name, value)

    def table(self, name: str, symbols: tuple[str, ...]) -> pd.DataFrame:
        if len(set(symbols)) != len(symbols) or not set(symbols).issubset(self.ordered_symbols):
            raise ValueError("Query assets must belong to the frozen basket")
        if name not in self._semantics:
            raise ValueError(f"DEFINITION_UNAVAILABLE: no frozen semantics for {name}")
        if name not in self._tables:
            raise ValueError(f"TEMPORAL_EVIDENCE_MISSING: {name} is an execution reference, not a disclosure table")
        table = self._tables[name]
        column = self._semantics[name].rules.symbol_column
        if column:
            table = table.loc[table[column].isin(symbols)]
        return _copy_frame(table).reset_index(drop=True)

    def bars(self, symbols: tuple[str, ...], start: date) -> pd.DataFrame:
        table = self.table("prices", symbols)
        return table.loc[pd.to_datetime(table.date).dt.date >= start].reset_index(drop=True)

    def realized_splits(self, symbols: tuple[str, ...]) -> pd.DataFrame:
        """Occurred split facts, not advance corporate-action disclosures."""
        if not self._split_semantics_valid:
            raise ValueError("DEFINITION_UNAVAILABLE: realized splits need frozen daily execution semantics")
        if len(set(symbols)) != len(symbols) or not set(symbols).issubset(self.ordered_symbols):
            raise ValueError("Query assets must belong to the frozen basket")
        return _copy_frame(self._realized_splits.loc[self._realized_splits.symbol.isin(symbols)]).reset_index(drop=True)


def _bind_point_in_time(tables: Mapping[str, pd.DataFrame], binding: AssetBinding, ordered_symbols: tuple[str, ...],
                        signal_cutoff: datetime, *, semantics: tuple[TableSemantics, ...] | None = None) -> PointInTimeView:
    cutoff = utc(signal_cutoff)
    if not ordered_symbols or len(set(ordered_symbols)) != len(ordered_symbols) or not set(ordered_symbols).issubset(item.local_symbol for item in binding.assets):
        raise ValueError("BINDING_MISMATCH: invalid ordered basket")
    resolved = semantics if semantics is not None else tuple(TableSemantics.from_binding(item) for item in binding.table_semantics)
    by_name = {item.binding.table: item for item in resolved}
    if len(by_name) != len(resolved) or {name: item.binding for name, item in by_name.items()} != {item.table: item for item in binding.table_semantics}:
        raise ValueError("BINDING_MISMATCH: reader declarations differ from frozen binding")
    events_semantics = by_name.get("events")
    prices_semantics = by_name.get("prices")
    split_semantics_valid = (events_semantics is not None and prices_semantics is not None
        and (events_semantics.binding.semantic_id, events_semantics.binding.version) == ("daily-events", "1")
        and events_semantics.rules.availability_rule == "execution_event"
        and prices_semantics.rules.price_mode in {"raw_cash", "adjusted_research"}
        and events_semantics.rules.price_mode == prices_semantics.rules.price_mode)
    realized = pd.DataFrame(columns=["symbol", "ex_date", "split_ratio", "source_available_at", "source_ref"])
    if split_semantics_valid:
        event_rows = tables.get("events")
        if event_rows is None or not {"symbol", "event_type", "ex_date", "split_ratio"}.issubset(event_rows.columns):
            raise ValueError("DATA_REQUIREMENT_UNSATISFIED: realized split facts missing")
        realized = _copy_frame(event_rows.loc[event_rows.event_type == "split", ["symbol", "ex_date", "split_ratio"]])
        if not realized.empty:
            realized["ex_date"] = pd.to_datetime(realized.ex_date)
            if realized.ex_date.isna().any():
                raise ValueError("TEMPORAL_EVIDENCE_MISSING: split ex-date missing")
            realized["source_available_at"] = pd.to_datetime([session_cutoff(day.date()) for day in realized.ex_date], utc=True)
            realized = realized.loc[realized.source_available_at <= cutoff].copy()
            realized["source_ref"] = [f"{binding.dataset_version_id}:events:" + canonical_hash({
                "symbol": row.symbol, "event_type": "split", "ex_date": row.ex_date.date().isoformat(),
            }) for row in realized.itertuples(index=False)]
        else:
            realized["source_available_at"] = pd.Series(dtype="datetime64[ns, UTC]")
            realized["source_ref"] = pd.Series(dtype="str")
    sessions = tuple(sorted(set(pd.to_datetime(tables["calendar"].date).dt.date)))
    visible = {}
    for name, definition in by_name.items():
        if definition.rules.availability_rule in {"execution_event", "declared_static_reference"}:
            continue
        frame = tables.get(name)
        if frame is None:
            raise ValueError(f"DATA_REQUIREMENT_UNSATISFIED: missing table {name}")
        symbol_column = definition.rules.symbol_column
        if symbol_column and symbol_column in frame.columns:
            frame = frame.loc[frame[symbol_column].isin(ordered_symbols)]
        visible[name] = _visible(frame, definition, cutoff, sessions, binding.dataset_version_id)
    return PointInTimeView(_ENGINE_VIEW_TOKEN, cutoff=cutoff, symbols=ordered_symbols, binding=binding,
                           tables=visible, semantics=by_name, realized_splits=realized.reset_index(drop=True),
                           split_semantics_valid=split_semantics_valid)
