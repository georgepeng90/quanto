from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def generate_sample_data(output_dir: str | Path, years: int = 6, seed: int = 7) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2018-01-02", periods=252 * years)
    symbols = ["512480", "159995", "512760", "515790", "512690", "512170"]
    rows: list[dict] = []
    for idx, symbol in enumerate(symbols):
        drift = 0.00015 + idx * 0.00002
        seasonal = np.sin(np.linspace(0, 8 * np.pi, len(dates)) + idx) * 0.0008
        returns = drift + seasonal + rng.normal(0, 0.014 + idx * 0.001, len(dates))
        close = 1.0 * np.cumprod(1 + returns)
        open_ = close * (1 + rng.normal(0, 0.003, len(dates)))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, len(dates))))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, len(dates))))
        volume = rng.integers(20_000_000, 120_000_000, len(dates))
        for date, o, h, l, c, v in zip(dates, open_, high, low, close, volume):
            rows.append({"date": date.date().isoformat(), "symbol": symbol, "open": o, "high": h, "low": l, "close": c, "volume": v, "amount": float(v * c)})

    bench_ret = rng.normal(0.00005, 0.012, len(dates))
    bench_close = 4000 * np.cumprod(1 + bench_ret)
    bench_amount = 800_000_000_000 + np.sin(np.linspace(0, 6 * np.pi, len(dates))) * 180_000_000_000
    bench_amount += rng.normal(0, 90_000_000_000, len(dates))
    benchmark = pd.DataFrame(
        {
            "date": [date.date().isoformat() for date in dates],
            "close": bench_close,
            "turnover": np.maximum(bench_amount, 450_000_000_000),
        }
    )

    prices_path = output / "prices.csv"
    benchmark_path = output / "benchmark.csv"
    pd.DataFrame(rows).to_csv(prices_path, index=False)
    benchmark.to_csv(benchmark_path, index=False)
    return prices_path, benchmark_path
