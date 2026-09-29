"""Synthetic storage pipeline timings, not an end-to-end strategy benchmark."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import tempfile
import time

import numpy as np
import pandas as pd

from quanto.config import BacktestSpec
from quanto.data import loaders
from quanto.workbench.models import FrozenRun
from quanto.workbench.snapshots import SnapshotBuilder
from quanto.workbench.store import WorkbenchStore


def fixture(root: Path, count: int, days: int) -> list[str]:
    root.mkdir()
    dates = pd.bdate_range("2010-01-04", periods=days + 1)
    symbols = [f"{index:06d}" for index in range(count)]
    close = np.random.default_rng(20260930).uniform(5, 50, count * days)
    prices = pd.DataFrame({"date": np.tile(dates[:days], count), "symbol": np.repeat(symbols, days),
        "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": 10000, "amount": close * 10000})
    tables = {"prices": prices,
        "benchmark": pd.DataFrame({"date": dates[:days], "close": np.arange(days) + 1000, "turnover": 1000000}),
        "calendar": pd.DataFrame({"date": dates}),
        "instruments": pd.DataFrame({"symbol": symbols, "lot_size": 100, "settlement_sessions": 1,
                                     "listing_date": dates[0], "group": "synthetic"}),
        "status": prices[["date", "symbol"]].assign(status="trading", can_buy=True, can_sell=True),
        "events": pd.DataFrame(columns=["symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"])}
    for name, table in tables.items():
        table.to_csv(root / f"{name}.csv", index=False)
    manifest = {"source": "synthetic storage benchmark", "is_demo": True, "price_mode": "raw_cash",
        "currency": "CNY", "calendar_start": str(dates[0].date()), "calendar_end": str(dates[-1].date()),
        "data_start": str(dates[0].date()), "data_end": str(dates[-2].date()),
        "volume_unit": "share", "amount_unit": "CNY", "event_coverage": "complete",
        "benchmark_type": "price_index", "field_semantics": {},
        "universe_rule": {"min_history_days": 120, "min_avg_turnover_20d": 0}, "limitations": []}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return symbols


def timed(operation):
    start = time.perf_counter()
    value = operation()
    return value, time.perf_counter() - start


def size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=100)
    parser.add_argument("--days", type=int, default=2500)
    args = parser.parse_args()
    if args.symbols < 5 or args.days < 250:
        parser.error("symbols >= 5 and days >= 250 required")
    with tempfile.TemporaryDirectory(prefix="quanto-pipeline-bench-") as directory:
        root = Path(directory)
        symbols = fixture(root / "input", args.symbols, args.days)
        bundle, load_seconds = timed(lambda: loaders.load_dataset(root / "input"))
        store = WorkbenchStore(root / "workspace")
        dataset, registration_seconds = timed(lambda: store.register_dataset(root / "input"))
        template = Path(__file__).resolve().parents[1] / "strategies" / "buy_hold"
        declaration = json.loads((template / "strategy.json").read_text(encoding="utf-8"))
        strategy = store.register_strategy({p.name: p.read_bytes() for p in template.iterdir() if p.is_file()}, declaration)
        spec = BacktestSpec.model_validate({"start": str(bundle.calendar.date.iloc[-251].date()),
            "end": str(bundle.manifest.data_end), "symbols": symbols[:5], "initial_cash": 100000, "seed": 7,
            "execution": {"model": "next_open", "commission_rate": 0, "minimum_commission": 0, "slippage_rate": 0, "max_stale_sessions": 0},
            "risk": {"max_total_weight": 1, "max_symbol_weight": 1, "group_caps": {}, "stress_scenarios": [], "pause_action": "block_new", "recovery_mode": "never"},
            "metrics": {"annual_sessions": 252, "risk_free_rate": 0, "cash_rate": 0, "definition_version": "1"}})
        draft = store.save_experiment(None, None, {"strategy_revision_id": strategy.id, "dataset_version_id": dataset.id,
            "strategy_params": {}, "backtest_spec": spec.model_dump(mode="json"), "sample_purpose": "storage benchmark"})
        builder = SnapshotBuilder(store)
        first, first_seconds = timed(lambda: builder.freeze(draft, "agent", None))
        _, second_seconds = timed(lambda: builder.freeze(draft, "agent", None))
        manifest, verification_seconds = timed(lambda: builder.verify(FrozenRun(first.run_id, first.manifest_path, first.manifest_hash), exact=False))
        if manifest["schema_version"] == 1:
            data_path = first.manifest_path.parent / "dataset"
            runtime, runtime_seconds = timed(lambda: loaders.load_dataset(data_path))
        else:
            data_path = store.root / "dataset_objects" / manifest["dataset_ref"]["content_hash"]
            runtime, runtime_seconds = timed(lambda: loaders.load_dataset_for_run(data_path, spec))
        expected = bundle.prices[bundle.prices.symbol.isin(spec.symbols) & (bundle.prices.date <= pd.Timestamp(spec.end))].reset_index(drop=True)
        actual = runtime.prices[runtime.prices.symbol.isin(spec.symbols) & (runtime.prices.date <= pd.Timestamp(spec.end))].reset_index(drop=True)
        pd.testing.assert_frame_equal(actual, expected)
        print(json.dumps({"platform": platform.platform(), "python": platform.python_version(),
            "rows": args.symbols * args.days, "schema": manifest["schema_version"],
            "method": "synthetic; one observation per pipeline phase; OS cache not cleared; not end-to-end",
            "seconds": {"csv_load_validate": load_seconds, "register": registration_seconds,
                "freeze_first": first_seconds, "freeze_second": second_seconds,
                "snapshot_verify": verification_seconds, "runtime_load": runtime_seconds},
            "bytes": {"input": size(root / "input"), "registered": size(dataset.path),
                "two_snapshots": size(store.root / "snapshots"), "shared_objects": size(store.root / "dataset_objects")},
            "loaded_price_rows": len(runtime.prices)}, indent=2))


if __name__ == "__main__":
    main()
