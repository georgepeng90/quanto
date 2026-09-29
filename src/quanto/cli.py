from __future__ import annotations

import argparse
import sys
from datetime import datetime

from quanto.data.fetcher import build_processed_dataset
from quanto.sample_data import generate_sample_data
from quanto.universe import load_universe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quanto")
    sub = parser.add_subparsers(dest="command", required=True)
    sample = sub.add_parser("generate-sample-data", help="Generate deterministic local processed sample data")
    sample.add_argument("--output-dir", default="data/processed")
    sample.add_argument("--years", type=int, default=6)
    sample.add_argument("--seed", type=int, default=7)
    sample.set_defaults(func=generate_sample_data_command)

    fetch = sub.add_parser("fetch-data", help="Fetch real ETF and benchmark data through AKShare with local cache")
    fetch.add_argument("--universe", default="configs/universe.yaml")
    fetch.add_argument("--start-date", default="2018-01-01")
    fetch.add_argument("--end-date", default=datetime.now().strftime("%Y-%m-%d"))
    fetch.add_argument("--data-dir", default="data/processed")
    fetch.add_argument("--raw-dir", default="data/raw")
    fetch.add_argument("--benchmark-symbol", default="sh000300")
    fetch.add_argument("--force-refresh", action="store_true")
    fetch.add_argument("--request-delay", type=float, default=1.0, help="Seconds to wait between ETF requests")
    fetch.add_argument("--max-retries", type=int, default=3)
    fetch.add_argument("--retry-delay", type=float, default=2.0)
    fetch.add_argument("--stop-on-error", action="store_true")
    fetch.add_argument("--quiet", action="store_true")
    fetch.set_defaults(func=fetch_data_command)

    return parser


def generate_sample_data_command(args: argparse.Namespace) -> int:
    prices, benchmark = generate_sample_data(args.output_dir, years=args.years, seed=args.seed)
    print(f"Sample prices written: {prices}")
    print(f"Sample benchmark written: {benchmark}")
    return 0


def fetch_data_command(args: argparse.Namespace) -> int:
    universe = load_universe(args.universe)
    result = build_processed_dataset(
        symbols=universe.symbols,
        start_date=args.start_date,
        end_date=args.end_date,
        processed_dir=args.data_dir,
        raw_dir=args.raw_dir,
        benchmark_symbol=args.benchmark_symbol,
        force_refresh=args.force_refresh,
        request_delay_seconds=args.request_delay,
        max_retries=args.max_retries,
        retry_delay_seconds=args.retry_delay,
        continue_on_error=not args.stop_on_error,
        log=not args.quiet,
    )
    print(f"Prices written: {result.prices_path} ({result.price_rows} rows)")
    print(f"Benchmark written: {result.benchmark_path} ({result.benchmark_rows} rows)")
    print(f"Fetch log written: {result.fetch_log_path}")
    if result.failed_symbols:
        print(f"Failed symbols skipped: {', '.join(result.failed_symbols)}")
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
