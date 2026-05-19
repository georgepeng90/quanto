from pathlib import Path

import pandas as pd

from quanto.backtest.engine import run_backtest
from quanto.config import load_config
from quanto.data.cache import load_processed_benchmark, load_processed_prices
from quanto.sample_data import generate_sample_data
from quanto.universe import apply_universe_filters, load_universe


def test_backtest_writes_expected_outputs(tmp_path: Path):
    data_dir = tmp_path / "processed"
    generate_sample_data(data_dir, years=6)
    cfg = load_config("configs/strategy.yaml")
    universe = load_universe("configs/universe.yaml")
    prices = apply_universe_filters(load_processed_prices(data_dir), universe)
    benchmark = load_processed_benchmark(data_dir)

    result = run_backtest(prices, benchmark, cfg.raw, tmp_path, "run")

    assert not result.equity_curve.empty
    assert set(["equity_curve.csv", "trades.csv", "positions.csv", "signals.csv", "metrics.json", "report.md"]).issubset(
        {path.name for path in (tmp_path / "run").iterdir()}
    )
    assert (pd.to_datetime(result.signals["trade_date"]) > pd.to_datetime(result.signals["signal_date"])).all()
    active_targets = result.signals[result.signals["target_weight"] > 0]
    if not active_targets.empty:
        assert (active_targets["target_weight"] <= 0.151).all()
