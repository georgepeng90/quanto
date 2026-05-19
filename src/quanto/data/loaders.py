from __future__ import annotations

from pathlib import Path

import pandas as pd

from quanto.data.schemas import BENCHMARK_COLUMNS, PRICE_COLUMNS


def _read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def load_prices(path: str | Path) -> pd.DataFrame:
    df = _read_table(path)
    missing = set(PRICE_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Price data missing columns: {sorted(missing)}")
    df = df[PRICE_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = df["symbol"].astype(str)
    numeric = ["open", "high", "low", "close", "volume", "amount"]
    df[numeric] = df[numeric].apply(pd.to_numeric, errors="coerce")
    df = df.sort_values(["date", "symbol"]).drop_duplicates(["date", "symbol"], keep="last")
    if df[numeric].isna().any().any():
        raise ValueError("Price data contains non-numeric or missing OHLCV values")
    return df


def load_benchmark(path: str | Path) -> pd.DataFrame:
    df = _read_table(path)
    missing = set(BENCHMARK_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Benchmark data missing columns: {sorted(missing)}")
    df = df[BENCHMARK_COLUMNS].copy()
    df["date"] = pd.to_datetime(df["date"])
    df[["close", "turnover"]] = df[["close", "turnover"]].apply(pd.to_numeric, errors="coerce")
    df = df.sort_values("date").drop_duplicates("date", keep="last")
    if df[["close", "turnover"]].isna().any().any():
        raise ValueError("Benchmark data contains non-numeric or missing values")
    return df
