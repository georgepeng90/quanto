"""Validate frozen stock execution evidence, independently of observation features."""
from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import math
from types import MappingProxyType
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quanto.framework import ResearchInterval
from quanto.data.point_in_time import STOCK_TABLE_VERSIONS


STOCK_DIVIDEND_LIMITATION = "Stock dividends are recorded before personal dividend tax; after-tax dividend returns are not modeled."
_TABLES = dict(zip(("fee_rules", "stock_open_status", "stock_sources"), STOCK_TABLE_VERSIONS))
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _require(condition, message):
    if not condition:
        raise ValueError("DATA_REQUIREMENT_UNSATISFIED: " + message)


def _day(value):
    _require(not pd.isna(value), "missing stock date")
    try:
        return pd.Timestamp(value).date()
    except (ValueError, TypeError) as exc:
        raise ValueError("DATA_REQUIREMENT_UNSATISFIED: invalid stock date") from exc


def _stamp(value):
    try:
        stamp = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("DATA_REQUIREMENT_UNSATISFIED: invalid stock timestamp") from exc
    _require(not pd.isna(stamp) and stamp.tzinfo is not None, "stock evidence needs timezone-aware availability")
    return stamp.tz_convert("UTC")


def _open(day):
    return pd.Timestamp(datetime.combine(day, time(9, 30), _SHANGHAI))


def _source_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"https", "http", "tcp"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and (parsed.scheme != "tcp" or parsed.port is not None and 0 < parsed.port <= 65535))
    except ValueError:
        return False


@dataclass(frozen=True)
class StockExecutionContext:
    instruments: Mapping
    open_status: Mapping
    fees: Mapping

    def fee_rule(self, symbol: str, trade_date: date, side: str) -> Mapping:
        rule = self.fees.get((symbol, trade_date, side))
        _require(rule is not None, f"no stock fee rule for {symbol} on {trade_date} ({side})")
        return rule


def validate_stock_data(bundle, binding, symbols: tuple[str, ...], interval: ResearchInterval) -> StockExecutionContext:
    manifest = bundle.manifest
    _require(manifest.price_mode == "raw_cash" and manifest.currency == "CNY", "stock execution requires raw_cash CNY")
    _require(manifest.volume_unit == "share" and manifest.amount_unit == "CNY", "stock units must be share/CNY")
    _require(manifest.event_coverage == "complete", "stock event coverage must be complete")
    _require(bundle.events.empty or set(bundle.events.event_type) <= {"dividend", "split"}, "unsupported stock event")
    declarations = {item.table: item for item in binding.table_semantics}
    for name, version in _TABLES.items():
        _require(name in bundle.auxiliary and name in declarations, f"missing stock table {name}")
        declaration = declarations[name]
        _require((declaration.semantic_id, declaration.version) == version, f"unsupported stock semantics: {name}")
        _require(declaration.temporal_rules.get("availability_rule") == "execution_event", f"{name} must stay outside observations")

    def rows(name, columns):
        frame = bundle.auxiliary[name]
        _require(set(columns.split()).issubset(frame.columns), f"missing columns in {name}")
        _require(not frame[columns.split()].isna().any().any(), f"missing facts in {name}")
        return frame.to_dict("records")

    sources = {}
    for row in rows("stock_sources", "source_ref source_url payload_base64 payload_sha256 published_at retrieved_at available_at coverage_start coverage_end"):
        ref = row["source_ref"]
        _require(isinstance(ref, str) and bool(ref.strip()) and ref not in sources, "duplicate/empty stock source")
        _require(_source_url(row["source_url"]), "source URL required")
        try:
            payload = base64.b64decode(row["payload_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("DATA_REQUIREMENT_UNSATISFIED: invalid stock source bytes") from exc
        _require(bool(payload) and hashlib.sha256(payload).hexdigest() == row["payload_sha256"], "stock source digest mismatch")
        published, retrieved, available = (_stamp(row[key]) for key in ("published_at", "retrieved_at", "available_at"))
        _require(published <= available <= retrieved, "stock source availability contradicts publication/retrieval")
        start, end = (_day(row[key]) for key in ("coverage_start", "coverage_end"))
        _require(start <= end, "invalid stock source coverage")
        sources[ref] = dict(row, available_at=available, coverage_start=start, coverage_end=end)

    def source_for(row, day, available):
        source = sources.get(row["source_ref"])
        _require(source is not None, "unknown stock source reference")
        _require(source["available_at"] <= available <= _open(day), "stock evidence unavailable at opening")
        _require(source["coverage_start"] <= day <= source["coverage_end"], "stock source does not cover execution date")

    event_source = sources.get(manifest.field_semantics.get("stock_event_coverage_source"))
    _require(event_source is not None, "complete stock event coverage needs source evidence")
    _require(event_source["coverage_start"] <= interval.start and event_source["coverage_end"] >= interval.end,
             "stock event evidence does not cover requested interval")
    # Event coverage can be established retrospectively. It is ledger evidence, never an observation.
    assets = {asset.local_symbol: asset.instrument_id for asset in binding.assets}
    instrument_rows = {row["symbol"]: row for row in bundle.instruments.to_dict("records")}
    instruments = {}
    for symbol in symbols:
        identity, row = assets.get(symbol), instrument_rows.get(symbol)
        _require(identity is not None and identity.instrument_type == "stock" and identity.exchange in {"SSE", "SZSE"}, "unsupported stock identity")
        _require(row is not None and row.get("board") == "main_board", "stock board must be explicitly main_board")
        _require(row["lot_size"] == 100 and row["settlement_sessions"] == 1, "stock requires 100-share lots and T+1 settlement")
        _require(_day(row["listing_date"]) <= interval.start, "stock not listed by interval start")
        instruments[symbol] = MappingProxyType(dict(row, instrument_id=identity.key))
    days = tuple(_day(value) for value in bundle.calendar.date if interval.start <= _day(value) <= interval.end)
    _require(bool(days), "stock interval has no trading sessions")
    rules_by_key = {}
    for row in rows("fee_rules", "exchange instrument_type board side effective_from effective_to stamp_duty_rate transfer_fee_rate available_at source_ref"):
        _require(row["exchange"] in {"SSE", "SZSE"} and row["instrument_type"] == "stock" and row["board"] == "main_board"
                 and row["side"] in {"buy", "sell"}, "unsupported stock fee rule")
        for key in ("stamp_duty_rate", "transfer_fee_rate"):
            value = row[key]
            _require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0, "invalid stock fee rate")
        _require(row["side"] != "buy" or row["stamp_duty_rate"] == 0, "buy stamp duty must be zero")
        start, end, available = _day(row["effective_from"]), _day(row["effective_to"]), _stamp(row["available_at"])
        _require(start < end and row["source_ref"] in sources, "invalid stock fee interval/source")
        key = (row["exchange"], row["side"])
        rules_by_key.setdefault(key, []).append(dict(row, effective_from=start, effective_to=end, available_at=available))
    for rules in rules_by_key.values():
        rules.sort(key=lambda row: row["effective_from"])
        _require(all(left["effective_to"] <= right["effective_from"] for left, right in zip(rules, rules[1:])), "overlapping stock fee intervals")
    fees = {}
    for symbol in symbols:
        for side in ("buy", "sell"):
            rules = rules_by_key.get((assets[symbol].exchange, side), ())
            cursor = 0
            for day in days:
                while cursor < len(rules) and rules[cursor]["effective_to"] <= day:
                    cursor += 1
                _require(cursor < len(rules) and rules[cursor]["effective_from"] <= day, "stock fee coverage gap")
                rule = rules[cursor]
                source_for(rule, day, rule["available_at"])
                fees[(symbol, day, side)] = MappingProxyType(rule)
    statuses = {}
    day_set = set(days)
    for row in rows("stock_open_status", "symbol date status can_buy can_sell available_at source_ref"):
        day, symbol = _day(row["date"]), row["symbol"]
        if symbol not in symbols or day not in day_set:
            continue
        key = (symbol, day)
        _require(key not in statuses, "duplicate stock opening status")
        _require(row["status"] in {"trading", "suspended"}, "unknown stock opening status")
        _require(all(isinstance(row[flag], (bool, np.bool_)) for flag in ("can_buy", "can_sell")), "stock permissions must be booleans")
        _require(row["status"] != "suspended" or not (row["can_buy"] or row["can_sell"]), "suspended stock cannot trade")
        source_for(row, day, _stamp(row["available_at"]))
        statuses[key] = MappingProxyType(dict(row, date=day))
    _require(len(statuses) == len(symbols) * len(days), "missing stock opening status")
    return StockExecutionContext(MappingProxyType(instruments), MappingProxyType(statuses), MappingProxyType(fees))
