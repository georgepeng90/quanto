from __future__ import annotations

from pathlib import Path
import json

import pandas as pd
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from quanto.config import BacktestSpec
from quanto.backtest.dataset_schemas import BENCHMARK_COLUMNS, INSTRUMENT_COLUMNS, PRICE_COLUMNS, DatasetBundle, DatasetManifest
from quanto.universe import build_eligibility


def _read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype={"symbol": str})
    raise ValueError(f"Unsupported data format: {path}")


def _require_columns(df: pd.DataFrame, columns: list[str], label: str) -> None:
    missing = set(columns) - set(df.columns)
    if missing:
        raise ValueError(f"{label} missing columns: {sorted(missing)}")


def _dates(df: pd.DataFrame, label: str) -> pd.DataFrame:
    out = df.copy(deep=True)
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    if out["date"].isna().any() or (out["date"] != out["date"].dt.normalize()).any():
        raise ValueError(f"{label} has invalid dates")
    return out


def _numbers(df: pd.DataFrame, columns: list[str], label: str) -> None:
    for column in columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if df[columns].isna().any().any() or not np.isfinite(df[columns].to_numpy(dtype=float)).all():
        raise ValueError(f"{label} has invalid numeric values")


def validate_prices(df: pd.DataFrame) -> pd.DataFrame:
    _require_columns(df, PRICE_COLUMNS, "prices")
    out = _dates(df, "prices")
    out["symbol"] = out["symbol"].astype(str)
    if out["symbol"].isin(["", "nan", "None"]).any():
        raise ValueError("prices has empty symbols")
    _numbers(out, PRICE_COLUMNS[2:], "prices")
    if out.duplicated(["date", "symbol"]).any():
        raise ValueError("prices has duplicate date/symbol keys")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("prices must be positive")
    if (out[["volume", "amount"]] < 0).any().any():
        raise ValueError("volume and amount must be nonnegative")
    if (out["high"] < out["low"]).any() or (out["high"] < out[["open", "close"]].max(axis=1)).any() or (out["low"] > out[["open", "close"]].min(axis=1)).any():
        raise ValueError("prices has inconsistent OHLC range")
    return out.sort_values(["date", "symbol"]).reset_index(drop=True)


def validate_benchmark(df: pd.DataFrame) -> pd.DataFrame:
    _require_columns(df, BENCHMARK_COLUMNS, "benchmark")
    out = _dates(df, "benchmark")
    _numbers(out, BENCHMARK_COLUMNS[1:], "benchmark")
    if out.duplicated("date").any() or (out["close"] <= 0).any() or (out["turnover"] < 0).any():
        raise ValueError("benchmark has invalid or duplicate rows")
    return out.sort_values("date").reset_index(drop=True)


def load_prices(path: str | Path) -> pd.DataFrame:
    return validate_prices(_read_table(path))


def load_benchmark(path: str | Path) -> pd.DataFrame:
    return validate_benchmark(_read_table(path))


def _dataset_table(root: Path, name: str) -> pd.DataFrame:
    for suffix in (".parquet", ".csv"):
        path = root / f"{name}{suffix}"
        if path.exists():
            return _read_table(path)
    raise FileNotFoundError(root / f"{name}.csv")


def load_dataset(path: Path) -> DatasetBundle:
    root = Path(path)
    manifest = DatasetManifest.model_validate(json.loads((root / "manifest.json").read_text(encoding="utf-8")))
    auxiliary = {name: _read_table(root / filename) for name, filename in manifest.auxiliary_tables.items()}
    return validate_dataset(DatasetBundle(
        manifest=manifest,
        prices=_dataset_table(root, "prices"),
        benchmark=_dataset_table(root, "benchmark"),
        calendar=_dataset_table(root, "calendar"),
        instruments=_dataset_table(root, "instruments"),
        status=_dataset_table(root, "status"),
        events=_dataset_table(root, "events"),
        eligibility=pd.DataFrame(),
        auxiliary=auxiliary,
    ))


def _run_table(root: Path, name: str, *, symbols: list[str] | None = None,
               end: pd.Timestamp | None = None) -> pd.DataFrame:
    path = root / f"{name}.parquet"
    if path.exists():
        schema = pq.read_schema(path)
        can_filter_symbols = symbols is None or pa.types.is_string(schema.field("symbol").type) or pa.types.is_large_string(schema.field("symbol").type)
        can_filter_date = end is None or pa.types.is_timestamp(schema.field("date").type)
        if can_filter_symbols and can_filter_date:
            filters = []
            if symbols is not None:
                filters.append(("symbol", "in", symbols))
            if end is not None:
                filters.append(("date", "<=", end))
            return pd.read_parquet(path, filters=filters or None)
    table = _dataset_table(root, name)
    if symbols is not None:
        table["symbol"] = table["symbol"].astype(str)
        table = table.loc[table["symbol"].isin(symbols)]
    if end is not None:
        table = _dates(table, name)
        table = table.loc[table["date"] <= end]
    return table.reset_index(drop=True)


def load_dataset_for_run(path: Path, spec: BacktestSpec) -> DatasetBundle:
    """Read a previously validated and hash-verified version for one run."""
    root = Path(path)
    manifest = DatasetManifest.model_validate(json.loads((root / "manifest.json").read_text(encoding="utf-8")))
    end = pd.Timestamp(spec.end)
    prices = validate_prices(_run_table(root, "prices", symbols=spec.symbols, end=end))
    benchmark = validate_benchmark(_run_table(root, "benchmark", end=end))
    calendar = _dates(_dataset_table(root, "calendar"), "calendar").sort_values("date").reset_index(drop=True)
    instruments = _dataset_table(root, "instruments")
    instruments["symbol"] = instruments["symbol"].astype(str)
    instruments["listing_date"] = pd.to_datetime(instruments["listing_date"])
    _numbers(instruments, ["lot_size", "settlement_sessions"], "instruments")
    status = _dates(_run_table(root, "status", symbols=spec.symbols, end=end), "status")
    status["symbol"] = status["symbol"].astype(str)
    events = _dataset_table(root, "events")
    events["symbol"] = events["symbol"].astype(str)
    events = events.loc[events["symbol"].isin(spec.symbols)].copy()
    for column in ("record_date", "ex_date", "pay_date"):
        events[column] = pd.to_datetime(events[column])
    auxiliary = {name: _read_table(root / filename) for name, filename in manifest.auxiliary_tables.items()}
    for table in auxiliary.values():
        table["available_at"] = pd.to_datetime(table["available_at"], utc=True)
    eligibility = build_eligibility(prices, instruments, manifest.universe_rule)
    return DatasetBundle(manifest, prices, benchmark, calendar, instruments, status, events.reset_index(drop=True),
        eligibility, auxiliary)


def validate_dataset(bundle: DatasetBundle) -> DatasetBundle:
    manifest = DatasetManifest.model_validate(bundle.manifest)
    prices = validate_prices(bundle.prices)
    benchmark = validate_benchmark(bundle.benchmark)
    _require_columns(bundle.calendar, ["date"], "calendar")
    calendar = _dates(bundle.calendar, "calendar")
    if calendar.empty or calendar.duplicated("date").any():
        raise ValueError("calendar is empty or has duplicate dates")
    calendar = calendar.sort_values("date").reset_index(drop=True)
    if calendar["date"].iloc[0].date() != manifest.calendar_start or calendar["date"].iloc[-1].date() != manifest.calendar_end:
        raise ValueError("calendar does not match manifest coverage")
    covered = set(calendar.loc[calendar["date"].between(pd.Timestamp(manifest.data_start), pd.Timestamp(manifest.data_end)), "date"])
    if not covered or set(benchmark["date"]) != covered:
        raise ValueError("benchmark must cover every data session")
    _require_columns(bundle.instruments, INSTRUMENT_COLUMNS, "instruments")
    instruments = bundle.instruments.copy(deep=True)
    instruments["symbol"] = instruments["symbol"].astype(str)
    instruments["listing_date"] = pd.to_datetime(instruments["listing_date"], errors="coerce")
    _numbers(instruments, ["lot_size", "settlement_sessions"], "instruments")
    if instruments.duplicated("symbol").any() or instruments["listing_date"].isna().any() or (instruments["lot_size"] <= 0).any() or (instruments["settlement_sessions"] < 0).any() or (instruments[["lot_size", "settlement_sessions"]] % 1 != 0).any().any() or instruments["group"].isna().any():
        raise ValueError("instruments has invalid rows")
    if not set(prices["symbol"]).issubset(set(instruments["symbol"])):
        raise ValueError("prices contain unregistered symbols")
    if not set(prices["date"]).issubset(covered):
        raise ValueError("price dates must appear in data coverage")
    _require_columns(bundle.status, ["date", "symbol", "status", "can_buy", "can_sell"], "status")
    status = _dates(bundle.status, "status")
    status["symbol"] = status["symbol"].astype(str)
    if status.duplicated(["date", "symbol"]).any() or not set(status["status"]).issubset({"trading", "suspended"}):
        raise ValueError("status has invalid or duplicate rows")
    if not set(status["symbol"]).issubset(set(instruments["symbol"])) or not set(status["date"]).issubset(covered):
        raise ValueError("status has unregistered symbols or dates")
    if any(not pd.api.types.is_bool_dtype(status[column]) for column in ("can_buy", "can_sell")):
        raise ValueError("status trading flags must be boolean")
    if status.loc[status["status"] == "suspended", ["can_buy", "can_sell"]].any().any():
        raise ValueError("suspended sessions cannot permit trading")
    for instrument in instruments.itertuples(index=False):
        active_sessions = covered & set(calendar.loc[calendar["date"] >= instrument.listing_date, "date"])
        quoted = set(prices.loc[prices["symbol"] == instrument.symbol, "date"])
        if any(day < instrument.listing_date for day in quoted):
            raise ValueError(f"price precedes listing date for {instrument.symbol}")
        suspended = set(status.loc[(status["symbol"] == instrument.symbol) & (status["status"] == "suspended"), "date"])
        symbol_status = status.loc[status["symbol"] == instrument.symbol]
        if not active_sessions.issubset(set(symbol_status["date"])):
            raise ValueError(f"missing trading status for {instrument.symbol}")
        if any(day not in quoted and day not in suspended for day in active_sessions):
            raise ValueError(f"unknown price gap for {instrument.symbol}")
        if quoted & suspended:
            raise ValueError(f"suspended session has a price for {instrument.symbol}")
    event_columns = ["symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"]
    _require_columns(bundle.events, event_columns, "events")
    events = bundle.events.copy(deep=True)
    events["symbol"] = events["symbol"].astype(str)
    if not set(events["symbol"]).issubset(set(instruments["symbol"])):
        raise ValueError("events contain unregistered symbols")
    if not set(events["event_type"]).issubset({"dividend", "split"}):
        raise ValueError("unknown event type")
    for column in ("record_date", "ex_date", "pay_date"):
        events[column] = pd.to_datetime(events[column], errors="coerce")
    if not events.empty:
        if events[["record_date", "ex_date"]].isna().any().any() or (events["record_date"] >= events["ex_date"]).any():
            raise ValueError("event record_date must precede ex_date")
        if events.duplicated(["symbol", "event_type", "ex_date"]).any():
            raise ValueError("events have duplicate symbol/type/ex_date keys")
        if not set(events["record_date"]).issubset(covered) or not set(events["ex_date"]).issubset(covered):
            raise ValueError("event dates must appear in data coverage")
        dividends = events[events["event_type"] == "dividend"]
        if dividends["pay_date"].isna().any() or (dividends["ex_date"] > dividends["pay_date"]).any():
            raise ValueError("dividend pay_date must follow ex_date")
        if not set(dividends["pay_date"]).issubset(covered):
            raise ValueError("dividend pay dates must appear in data coverage")
        for kind, column in (("dividend", "cash_per_share"), ("split", "split_ratio")):
            values = pd.to_numeric(events.loc[events["event_type"] == kind, column], errors="coerce")
            if values.isna().any() or not np.isfinite(values.to_numpy(dtype=float)).all() or (values <= 0).any():
                raise ValueError(f"invalid {kind} {column}")
    auxiliary = {name: table.copy(deep=True) for name, table in bundle.auxiliary.items()}
    for name, table in auxiliary.items():
        _require_columns(table, ["available_at"], f"auxiliary {name}")
        table["available_at"] = pd.to_datetime(table["available_at"], utc=True, errors="coerce")
        if table["available_at"].isna().any():
            raise ValueError(f"auxiliary {name} has invalid available_at")
    eligibility = build_eligibility(prices, instruments, manifest.universe_rule)
    return DatasetBundle(manifest, prices, benchmark, calendar, instruments, status, events, eligibility, auxiliary)
