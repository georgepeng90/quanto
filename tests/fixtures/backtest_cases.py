from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def dataset_case(root: Path, price_mode: str = "raw_cash") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2024-01-01", periods=5)
    pd.DataFrame(
        {
            "date": dates[:3],
            "symbol": ["000001"] * 3,
            "open": [1.0, 1.1, 1.2],
            "high": [1.1, 1.2, 1.3],
            "low": [0.9, 1.0, 1.1],
            "close": [1.0, 1.1, 1.2],
            "volume": [1000] * 3,
            "amount": [100_000_000] * 3,
        }
    ).to_csv(root / "prices.csv", index=False)
    pd.DataFrame({"date": dates[:3], "close": [4000, 4001, 4002], "turnover": [1e9] * 3}).to_csv(root / "benchmark.csv", index=False)
    pd.DataFrame({"date": dates}).to_csv(root / "calendar.csv", index=False)
    pd.DataFrame({"symbol": ["000001"], "lot_size": [100], "settlement_sessions": [1], "listing_date": ["2024-01-01"], "group": ["industry"]}).to_csv(root / "instruments.csv", index=False)
    pd.DataFrame({"date": dates[:3], "symbol": ["000001"] * 3, "status": ["trading"] * 3, "can_buy": [True] * 3, "can_sell": [True] * 3}).to_csv(root / "status.csv", index=False)
    pd.DataFrame(columns=["symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"]).to_csv(root / "events.csv", index=False)
    manifest = {
        "source": "test fixture",
        "is_demo": False,
        "price_mode": price_mode,
        "currency": "CNY",
        "calendar_start": "2024-01-01",
        "calendar_end": "2024-01-05",
        "data_start": "2024-01-01",
        "data_end": "2024-01-03",
        "volume_unit": "share",
        "amount_unit": "CNY",
        "event_coverage": "complete",
        "benchmark_type": "price_index",
        "field_semantics": {"amount": "CNY turnover"},
        "universe_rule": {"min_history_days": 3, "min_avg_turnover_20d": 0},
        "limitations": ["Adjusted prices are for research; cash distributions are not replayed."] if price_mode == "adjusted_research" else [],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root
