"""Small synthetic storage benchmark; results describe warm local reads only."""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow


def measure(reader, expected: pd.DataFrame) -> float:
    result = reader()  # warm-up
    pd.testing.assert_frame_equal(result, expected, check_exact=True)
    samples = []
    for _ in range(3):
        started = time.perf_counter()
        result = reader()
        samples.append(time.perf_counter() - started)
        pd.testing.assert_frame_equal(result, expected, check_exact=True)
    return statistics.median(samples)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=100)
    parser.add_argument("--days", type=int, default=2500)
    args = parser.parse_args()
    if args.symbols < 5 or args.days < 250:
        parser.error("symbols must be >= 5 and days must be >= 250")

    dates = pd.date_range("2010-01-01", periods=args.days, freq="B")
    symbols = [f"S{i:04d}" for i in range(args.symbols)]
    rng = np.random.default_rng(20260930)
    close = rng.uniform(5, 500, size=(args.symbols, args.days)).astype("float64")
    rows = args.symbols * args.days
    frame = pd.DataFrame({
        "symbol": np.repeat(symbols, args.days),
        "date": np.tile(dates.to_numpy(), args.symbols),
        "open": close.ravel() * rng.uniform(0.99, 1.01, rows),
        "high": close.ravel() * 1.02,
        "low": close.ravel() * 0.98,
        "close": close.ravel(),
        "volume": rng.integers(1000, 10_000_000, rows, dtype=np.int64),
    })
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values(["symbol", "date"], ignore_index=True)
    query_symbols = symbols[:5]
    query_start = dates[-250]
    query_end = dates[-1]
    query_expected = frame.loc[
        frame["symbol"].isin(query_symbols)
        & frame["date"].between(query_start, query_end)
    ].reset_index(drop=True)
    columns = list(frame.columns)

    with tempfile.TemporaryDirectory(prefix="quanto-storage-bench-") as temp:
        root = Path(temp)
        csv_path, parquet_path, sqlite_path = (
            root / "market.csv", root / "market.parquet", root / "market.sqlite3"
        )
        frame.to_csv(csv_path, index=False)
        frame.to_parquet(parquet_path, index=False)
        connection = sqlite3.connect(sqlite_path)
        with connection:
            frame.to_sql("ohlcv", connection, index=False)
            connection.execute("CREATE INDEX idx_ohlcv_symbol_date ON ohlcv(symbol, date)")
        connection.close()

        def read_csv(full: bool) -> pd.DataFrame:
            result = pd.read_csv(csv_path, parse_dates=["date"], float_precision="round_trip")
            if not full:
                result = result.loc[
                    result["symbol"].isin(query_symbols)
                    & result["date"].between(query_start, query_end)
                ]
            return result.loc[:, columns].reset_index(drop=True)

        def read_parquet(full: bool) -> pd.DataFrame:
            filters = None if full else [
                ("symbol", "in", query_symbols),
                ("date", ">=", query_start.to_pydatetime()),
                ("date", "<=", query_end.to_pydatetime()),
            ]
            return pd.read_parquet(parquet_path, columns=columns, filters=filters).reset_index(drop=True)

        def read_sqlite(full: bool) -> pd.DataFrame:
            connection = sqlite3.connect(sqlite_path)
            try:
                if full:
                    result = pd.read_sql_query(
                        "SELECT symbol,date,open,high,low,close,volume FROM ohlcv ORDER BY symbol,date",
                        connection, parse_dates=["date"],
                    )
                else:
                    marks = ",".join("?" for _ in query_symbols)
                    result = pd.read_sql_query(
                        f"SELECT symbol,date,open,high,low,close,volume FROM ohlcv "
                        f"WHERE symbol IN ({marks}) AND date BETWEEN ? AND ? ORDER BY symbol,date",
                        connection,
                        params=[*query_symbols, query_start.strftime("%Y-%m-%d"), query_end.strftime("%Y-%m-%d 23:59:59")],
                        parse_dates=["date"],
                    )
            finally:
                connection.close()
            return result.loc[:, columns].reset_index(drop=True)

        full_readers = {
            "csv": lambda: read_csv(True),
            "parquet": lambda: read_parquet(True),
            "sqlite": lambda: read_sqlite(True),
        }
        query_readers = {
            "csv": lambda: read_csv(False),
            "parquet": lambda: read_parquet(False),
            "sqlite": lambda: read_sqlite(False),
        }
        results = {}
        sizes = {"csv": csv_path.stat().st_size, "parquet": parquet_path.stat().st_size,
                 "sqlite": sqlite_path.stat().st_size}
        for name, reader in full_readers.items():
            results[name] = {
                "bytes": sizes[name],
                "full_read_median_seconds": measure(reader, frame),
                "filtered_read_median_seconds": measure(query_readers[name], query_expected),
                "full_rows": rows,
                "filtered_rows": len(query_expected),
            }
        print(json.dumps({
            "platform": platform.platform(),
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "numpy": np.__version__,
            "pyarrow": pyarrow.__version__,
            "seed": 20260930,
            "symbols": args.symbols,
            "business_days": args.days,
            "rows": rows,
            "query": {"symbols": query_symbols, "days": 250},
            "method": "synthetic OHLCV; one warm-up + three timed reads; median; temporary files; warm local cache",
            "results": results,
        }, indent=2))


if __name__ == "__main__":
    main()
