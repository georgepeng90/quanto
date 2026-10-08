from __future__ import annotations

from pathlib import Path
import json

import numpy as np
import pandas as pd


SAMPLE_INSTRUMENTS = (
    ("SSE", "512480"), ("SZSE", "159995"), ("SSE", "512760"),
    ("SSE", "515790"), ("SSE", "512690"), ("SSE", "512170"),
)


def generate_sample_data(output_dir: str | Path, years: int = 6, seed: int = 7) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2018-01-02", periods=252 * years)
    symbols = [code for _, code in SAMPLE_INSTRUMENTS]
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
    pd.DataFrame({"date": dates.date}).to_csv(output / "calendar.csv", index=False)
    pd.DataFrame({
        "symbol": symbols,
        "lot_size": [100] * len(symbols),
        "settlement_sessions": [1] * len(symbols),
        "listing_date": [dates[0].date().isoformat()] * len(symbols),
        "group": ["sample"] * len(symbols),
    }).to_csv(output / "instruments.csv", index=False)
    pd.DataFrame({"date": np.tile(dates.date, len(symbols)), "symbol": np.repeat(symbols, len(dates)), "status": "trading", "can_buy": True, "can_sell": True}).to_csv(output / "status.csv", index=False)
    pd.DataFrame(columns=["symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"]).to_csv(output / "events.csv", index=False)
    manifest = {
        "source": "quanto deterministic synthetic sample",
        "is_demo": True,
        "price_mode": "adjusted_research",
        "currency": "CNY",
        "calendar_start": dates[0].date().isoformat(),
        "calendar_end": dates[-1].date().isoformat(),
        "data_start": dates[0].date().isoformat(),
        "data_end": dates[-1].date().isoformat(),
        "volume_unit": "share",
        "amount_unit": "CNY",
        "event_coverage": "unknown",
        "benchmark_type": "price_index",
        "field_semantics": {"amount": "synthetic CNY turnover", "close": "synthetic price"},
        "universe_rule": {"min_history_days": 1, "min_avg_turnover_20d": 0},
        "limitations": ["Synthetic adjusted research prices do not replay cash distributions or corporate actions."],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return prices_path, benchmark_path
