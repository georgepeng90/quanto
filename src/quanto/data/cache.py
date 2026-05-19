from __future__ import annotations

from pathlib import Path

import pandas as pd

REQUIRED_PRICE_COLUMNS = {"date", "symbol", "open", "high", "low", "close", "volume", "amount"}
REQUIRED_BENCHMARK_COLUMNS = {"date", "close"}


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported data file format: {path}")


def _first_existing(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    raise FileNotFoundError("None of these data files exist: " + ", ".join(map(str, paths)))


def normalize_prices(df: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED_PRICE_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Price data missing columns: {sorted(missing)}")
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["symbol"] = out["symbol"].astype(str)
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date", "symbol", "open", "close"])
    return out.sort_values(["date", "symbol"]).reset_index(drop=True)


def normalize_benchmark(df: pd.DataFrame) -> pd.DataFrame:
    if "turnover" not in df.columns and "amount" in df.columns:
        df = df.rename(columns={"amount": "turnover"})
    missing = REQUIRED_BENCHMARK_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Benchmark data missing columns: {sorted(missing)}")
    if "turnover" not in df.columns:
        raise ValueError("Benchmark data missing columns: ['turnover'] or ['amount']")
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out["turnover"] = pd.to_numeric(out["turnover"], errors="coerce")
    return out.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def load_processed_prices(data_dir: str | Path) -> pd.DataFrame:
    data_dir = Path(data_dir)
    path = _first_existing([data_dir / "prices.parquet", data_dir / "prices.csv"])
    return normalize_prices(_read_table(path))


def load_processed_benchmark(data_dir: str | Path) -> pd.DataFrame:
    data_dir = Path(data_dir)
    path = _first_existing([data_dir / "benchmark.parquet", data_dir / "benchmark.csv"])
    return normalize_benchmark(_read_table(path))


def write_processed_prices(prices: pd.DataFrame, data_dir: str | Path) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "prices.csv"
    normalize_prices(prices).to_csv(path, index=False)
    return path


def write_processed_benchmark(benchmark: pd.DataFrame, data_dir: str | Path) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "benchmark.csv"
    normalize_benchmark(benchmark).to_csv(path, index=False)
    return path


def append_fetch_log(data_dir: str | Path, rows: list[dict]) -> Path:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "fetch_log.csv"
    existing = pd.read_csv(path) if path.exists() else pd.DataFrame()
    pd.concat([existing, pd.DataFrame(rows)], ignore_index=True).to_csv(path, index=False)
    return path
