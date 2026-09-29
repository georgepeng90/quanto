from __future__ import annotations

from dataclasses import replace
import json

import pandas as pd
from pandas.testing import assert_frame_equal

from quanto.backtest.engine import run_backtest
from quanto.data.loaders import load_dataset, validate_dataset
from quanto.strategies.base import Hold, TargetPortfolio
from tests.fixtures.backtest_cases import dataset_case
from tests.fixtures.engine_cases import bundle_case, spec_case


def test_parquet_roundtrip_preserves_columns_symbols_empty_events_and_utc_auxiliary(tmp_path):
    from quanto.data.storage import write_parquet_dataset
    source = dataset_case(tmp_path / "source")
    prices = pd.read_csv(source / "prices.csv", dtype={"symbol": str})
    prices["vendor_note"] = ["first", "second", "third"]
    prices.to_csv(source / "prices.csv", index=False)
    approvals = pd.DataFrame([{"approval_id": "a", "pause_asof": "2024-01-02", "available_at": "2024-01-02T06:00:00Z"}])
    approvals.to_csv(source / "approvals.csv", index=False)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["auxiliary_tables"] = {"risk_approvals": "approvals.csv"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original = load_dataset(source)

    destination = tmp_path / "parquet"
    write_parquet_dataset(original, destination)
    loaded = load_dataset(destination)

    assert set(path.name for path in destination.iterdir()) == {
        "manifest.json", "prices.parquet", "benchmark.parquet", "calendar.parquet",
        "instruments.parquet", "status.parquet", "events.parquet", "auxiliary_0000.parquet",
    }
    assert loaded.manifest.auxiliary_tables == {"risk_approvals": "auxiliary_0000.parquet"}
    for name in ("prices", "benchmark", "calendar", "instruments", "status", "events", "eligibility"):
        assert_frame_equal(getattr(loaded, name), getattr(original, name), check_dtype=False)
    assert_frame_equal(loaded.auxiliary["risk_approvals"], original.auxiliary["risk_approvals"])
    assert loaded.prices["symbol"].tolist() == ["000001"] * 3
    assert "vendor_note" in loaded.prices
    assert original.manifest.auxiliary_tables == {"risk_approvals": "approvals.csv"}


def test_selective_load_keeps_long_history_calendar_and_cross_boundary_events(tmp_path):
    from quanto.data.loaders import load_dataset_for_run
    from quanto.data.storage import write_parquet_dataset
    base = bundle_case(symbols=("A", "B"))
    dates = pd.bdate_range("2024-01-01", periods=25)
    prices = pd.DataFrame([
        dict(date=day, symbol=symbol, open=10, high=10, low=10, close=10, volume=10000, amount=100000)
        for day in dates for symbol in ("A", "B")
    ])
    status = prices[["date", "symbol"]].assign(status="trading", can_buy=True, can_sell=True)
    benchmark = pd.DataFrame({"date": dates, "close": 10, "turnover": 100})
    calendar = pd.DataFrame({"date": pd.bdate_range(dates[0], periods=27)})
    events = pd.DataFrame([
        dict(symbol="A", event_type="dividend", record_date=dates[21], ex_date=dates[22], pay_date=dates[23], cash_per_share=1, split_ratio=1),
        dict(symbol="A", event_type="split", record_date=dates[22], ex_date=dates[23], pay_date=pd.NaT, cash_per_share=0, split_ratio=2),
        dict(symbol="B", event_type="split", record_date=dates[22], ex_date=dates[23], pay_date=pd.NaT, cash_per_share=0, split_ratio=2),
    ])
    manifest = base.manifest.model_copy(update={"calendar_end": calendar.date.iloc[-1].date(), "data_end": dates[-1].date(),
        "universe_rule": {"min_history_days": 20, "min_avg_turnover_20d": 0}})
    full = validate_dataset(replace(base, manifest=manifest, prices=prices, benchmark=benchmark, calendar=calendar,
        status=status, events=events))
    spec = spec_case(full, start=dates[20].date(), end=dates[23].date(), symbols=["A"])
    destination = tmp_path / "parquet"
    write_parquet_dataset(full, destination)

    selected = load_dataset_for_run(destination, spec)
    assert set(selected.prices.symbol) == {"A"}
    assert selected.prices.date.min() == dates[0]
    assert selected.prices.date.max() == dates[23]
    assert selected.eligibility.loc[selected.eligibility.date == dates[19], "eligible"].iloc[0]
    assert selected.calendar.date.iloc[-1] == calendar.date.iloc[-1]
    assert set(selected.events.symbol) == {"A"}
    assert len(selected.events) == 2

    class LongHistoryStrategy:
        warmup_bars = 1

        def should_evaluate(self, ctx):
            return True

        def on_close(self, ctx):
            assert ctx.history.date.min() == dates[0]
            return TargetPortfolio({"A": 1}, {}, {}) if ctx.asof == dates[20] else Hold("wait")

    old = run_backtest(full, LongHistoryStrategy(), spec, lambda event: None, lambda: False)
    new = run_backtest(selected, LongHistoryStrategy(), spec, lambda event: None, lambda: False)
    for name in ("signals", "orders", "fills", "positions", "equity", "account_events", "risk_events"):
        assert_frame_equal(getattr(new, name), getattr(old, name), check_dtype=False)


def test_selective_load_accepts_legacy_csv_dataset(tmp_path):
    from quanto.data.loaders import load_dataset_for_run

    source = dataset_case(tmp_path / "source")
    full = load_dataset(source)
    spec = spec_case(full, start="2024-01-02", end="2024-01-03")
    selected = load_dataset_for_run(source, spec)

    assert_frame_equal(selected.prices, full.prices)
    assert_frame_equal(selected.eligibility, full.eligibility)
    assert_frame_equal(selected.calendar, full.calendar)
    assert selected.events.empty
