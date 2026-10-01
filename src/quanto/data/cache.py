from __future__ import annotations

from pathlib import Path

import pandas as pd

from quanto.backtest.dataset_loaders import load_benchmark, load_prices, validate_benchmark, validate_prices


def _first_existing(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    raise FileNotFoundError("None of these data files exist: " + ", ".join(map(str, paths)))


def normalize_prices(df: pd.DataFrame) -> pd.DataFrame:
    return validate_prices(df)


def normalize_benchmark(df: pd.DataFrame) -> pd.DataFrame:
    return validate_benchmark(df)


def load_processed_prices(data_dir: str | Path) -> pd.DataFrame:
    data_dir = Path(data_dir)
    path = _first_existing([data_dir / "prices.parquet", data_dir / "prices.csv"])
    return load_prices(path)


def load_processed_benchmark(data_dir: str | Path) -> pd.DataFrame:
    data_dir = Path(data_dir)
    path = _first_existing([data_dir / "benchmark.parquet", data_dir / "benchmark.csv"])
    return load_benchmark(path)


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
