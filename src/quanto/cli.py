from __future__ import annotations

import argparse
import sys
from datetime import datetime

from quanto.config import load_config
from quanto.data.cache import load_processed_benchmark, load_processed_prices
from quanto.sample_data import generate_sample_data
from quanto.universe import apply_universe_filters, load_universe
from quanto.backtest.engine import run_backtest


def run_backtest_command(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    prices = load_processed_prices(args.data_dir or cfg.data_dir)
    benchmark = load_processed_benchmark(args.data_dir or cfg.data_dir)
    universe = load_universe(args.universe)
    prices = apply_universe_filters(prices, universe)
    if prices.empty:
        raise ValueError("No price rows remain after universe filters.")
    run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    result = run_backtest(prices, benchmark, cfg.raw, args.output_dir or cfg.output_dir, run_id)
    print(f"Backtest completed: {result.run_dir}")
    print(f"Final equity: {result.metrics['final_equity']:.2f}")
    print(f"Max drawdown: {result.metrics['max_drawdown']:.2%}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quanto")
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("generate-sample-data", help="Generate deterministic local processed sample data")
    sample.add_argument("--output-dir", default="data/processed")
    sample.add_argument("--years", type=int, default=6)
    sample.add_argument("--seed", type=int, default=7)
    sample.set_defaults(func=generate_sample_data_command)

    bt = sub.add_parser("run-backtest", help="Run pandas weekly rotation backtest")
    bt.add_argument("--config", default="configs/strategy.yaml")
    bt.add_argument("--universe", default="configs/universe.yaml")
    bt.add_argument("--data-dir", default=None)
    bt.add_argument("--output-dir", default=None)
    bt.add_argument("--run-id", default=None)
    bt.set_defaults(func=run_backtest_command)
    return parser


def generate_sample_data_command(args: argparse.Namespace) -> int:
    prices, benchmark = generate_sample_data(args.output_dir, years=args.years, seed=args.seed)
    print(f"Sample prices written: {prices}")
    print(f"Sample benchmark written: {benchmark}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
