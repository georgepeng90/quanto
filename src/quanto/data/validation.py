from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from quanto.data.contracts import BAR_COLUMNS, BAR_KEY, BAR_ORDER, MarketDataError, NormalizedChunk


REFERENCE_COLUMNS = {
    "calendar": ("trading_date", "session_start", "session_end"),
    "instruments": ("instrument_id", "listing_date"),
    "trading_status": ("instrument_id", "trading_date", "status"),
    "corporate_actions": ("instrument_id", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"),
}
REFERENCE_KEYS = {"calendar": ("session_start", "session_end"), "trading_status": ("instrument_id", "trading_date"), "instruments": ("instrument_id",)}


def aware_column(values: pd.Series) -> pd.Series:
    if any(pd.Timestamp(value).tzinfo is None for value in values.dropna()):
        raise ValueError("timezone-aware timestamps required")
    return pd.to_datetime(values, utc=True)


def deduplicate(frame: pd.DataFrame, keys: tuple[str, ...]) -> pd.DataFrame:
    frame = frame.drop_duplicates()
    if frame.duplicated(list(keys)).any():
        raise MarketDataError("SEMANTIC_CONFLICT", "different records at identical market key")
    return frame


def merge_reference(frame: pd.DataFrame, keys: tuple[str, ...]) -> pd.DataFrame:
    frame = frame.drop_duplicates()
    repeated = frame.duplicated(list(keys), keep=False)
    if not repeated.any():
        return frame.infer_objects()
    groups = frame.loc[repeated].groupby(list(keys), sort=False, dropna=False)
    if (groups.nunique(dropna=True) > 1).any().any():
        raise MarketDataError("SEMANTIC_CONFLICT", "different known reference facts at identical market key")
    return pd.concat([frame.loc[~repeated], groups.first().reset_index()], ignore_index=True)[list(frame.columns)].infer_objects()


def validate_chunk(chunk: NormalizedChunk) -> NormalizedChunk:
    request, frame = chunk.request, chunk.frame.copy()
    try:
        required = BAR_COLUMNS[:4] if request.kind == "bars" else REFERENCE_COLUMNS.get(request.kind, ("available_at",))
        missing = set(required).union(request.columns) - set(frame.columns)
        if missing:
            raise ValueError(f"missing fields: {sorted(missing)}")
        for column in ("bar_start", "bar_end", "session_start", "session_end", "available_at"):
            if column in frame:
                frame[column] = aware_column(frame[column])
        for column in ("trading_date", "listing_date", "record_date", "ex_date", "pay_date"):
            if column in frame:
                frame[column] = pd.to_datetime(frame[column]).dt.date
        if "instrument_id" in frame:
            allowed = {instrument.key for instrument in request.instruments}
            if allowed and not set(frame.instrument_id).issubset(allowed):
                raise ValueError("unrequested instrument")
        if request.kind == "bars":
            if request.price_basis.mode == "vendor_research" and not request.price_basis.basis_id:
                raise ValueError("normalized bars require a resolved research basis")
            if frame[list(BAR_COLUMNS[:4])].isna().any().any() or ((frame.bar_start < request.start) | (frame.bar_end > request.end) | (frame.bar_start >= frame.bar_end)).any():
                raise ValueError("invalid bar interval")
            if request.frequency == "1m" and ((frame.bar_end - frame.bar_start) != pd.Timedelta(minutes=1)).any():
                raise ValueError("minute bars must cover exactly one minute")
            for column in BAR_COLUMNS[4:]:
                if column not in frame:
                    frame[column] = np.nan
                frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
                nonnull = frame[column].dropna()
                if not np.isfinite(nonnull).all() or (nonnull < 0).any() or (column in BAR_COLUMNS[4:8] and (nonnull == 0).any()):
                    raise ValueError(f"invalid {column}")
            if ((frame.high < frame[["open", "close", "low"]].max(axis=1)) | (frame.low > frame[["open", "close", "high"]].min(axis=1))).any():
                raise ValueError("OHLC bounds invalid")
            if "units" in chunk.source.verified:
                for column, unit in (("volume", "share"), ("amount", request.currency)):
                    if frame[column].notna().any() and chunk.source.units.get(column) != unit:
                        raise ValueError("verified units must use shares and the declared currency")
            frame = deduplicate(frame, BAR_KEY).sort_values(list(BAR_ORDER), kind="stable")
        elif request.kind == "calendar":
            if frame[["session_start", "session_end"]].isna().any().any() or (frame.session_start >= frame.session_end).any():
                raise ValueError("invalid market session")
            frame = deduplicate(frame, ("session_start", "session_end"))
        elif request.kind == "instruments":
            frame = deduplicate(frame, ("instrument_id",))
        elif request.kind == "trading_status":
            if not set(frame.status).issubset({"trading", "suspended", "not_listed", "delisted", "unknown"}):
                raise ValueError("unknown trading status")
            frame = deduplicate(frame, ("instrument_id", "trading_date"))
        return replace(chunk, frame=frame.reset_index(drop=True))
    except (ValueError, TypeError) as exc:
        raise MarketDataError("INCOMPLETE", str(exc)) from exc
