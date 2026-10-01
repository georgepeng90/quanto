from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs/strategy-research-20261001-round2/alternate-data"
OUTPUT = ROOT / "outputs/strategy-research-20261001-round2/fresh"
CUTOFF = pd.Timestamp("2026-09-30")
TABLES = ("prices", "benchmark", "calendar", "instruments", "status", "events", "eligibility")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(path: Path, frame: pd.DataFrame) -> dict:
    dates = frame["date"]
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "sha256": sha256(path),
        "rows": len(frame),
        "first_date": dates.min().date().isoformat(),
        "last_date": dates.max().date().isoformat(),
    }


def read_csv(path: Path, symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"symbol": str})
    if list(frame.columns) != ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]:
        raise ValueError(f"Unexpected CSV columns: {path}")
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame.empty or not frame["symbol"].eq(symbol).all():
        raise ValueError(f"Missing rows or wrong symbol: {path}")
    if frame["date"].duplicated().any() or not frame["date"].is_monotonic_increasing:
        raise ValueError(f"Duplicate or unsorted dates: {path}")
    if frame["date"].iloc[-1] != CUTOFF or frame["date"].gt(CUTOFF).any():
        raise ValueError(f"CSV does not end exactly at cutoff: {path}")
    return frame


def verify_legacy() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from quanto.backtest.dataset_loaders import load_dataset, validate_benchmark, validate_dataset, validate_prices
    from quanto.backtest.dataset_schemas import DatasetBundle, DatasetManifest

    selection_path = ROOT / "docs/research/2026-10-01/source_audit.json"
    instruments_path = ROOT / "outputs/strategy-research-20261001/dataset/instruments.parquet"
    requests_path = SOURCE / "tencent_requests.json"
    provider_audit_path = SOURCE / "tencent_audit.json"
    unit_check_path = SOURCE / "unit_check.json"
    adjustment_check_path = SOURCE / "adjustment_check.json"
    symbols = json.loads(selection_path.read_text(encoding="utf-8"))["selected_symbols"]
    if len(symbols) != 27 or len(set(symbols)) != 27:
        raise ValueError("Expected exactly 27 distinct selected symbols")
    source_dir = SOURCE / "tencent_qfq"
    available = {path.stem for path in source_dir.glob("*.csv") if path.name != "benchmark_sh000300.csv"}
    if available != set(symbols):
        raise ValueError(f"Source symbol mismatch: missing={sorted(set(symbols)-available)}, extra={sorted(available-set(symbols))}")

    requests = json.loads(requests_path.read_text(encoding="utf-8"))
    audit_rows = json.loads(provider_audit_path.read_text(encoding="utf-8"))["audits"]
    audits = {row["symbol"]: row for row in audit_rows}
    if set(audits) != set(symbols) | {"000300"}:
        raise ValueError("Provider audit symbol set differs")
    if json.loads(unit_check_path.read_text(encoding="utf-8"))["passed"] != 27:
        raise ValueError("Unit spot check did not pass for all ETFs")

    benchmark_source_path = source_dir / "benchmark_sh000300.csv"
    benchmark_source = read_csv(benchmark_source_path, "000300")
    benchmark = validate_benchmark(benchmark_source[["date", "close", "amount"]].rename(columns={"amount": "turnover"}))
    sessions = pd.Index(benchmark["date"])
    if benchmark["date"].iloc[0] != pd.Timestamp("2010-01-04"):
        raise ValueError("Unexpected benchmark calendar start")
    instruments_old = pd.read_parquet(instruments_path)
    instruments = instruments_old.set_index("symbol").loc[symbols].reset_index()
    if set(instruments_old["symbol"]) != set(symbols):
        raise ValueError("Prior instrument set differs from selected symbols")
    if not instruments["lot_size"].eq(100).all() or not instruments["settlement_sessions"].eq(1).all():
        raise ValueError("Prior lot or settlement assumption differs")

    source_records = [record(benchmark_source_path, benchmark_source)]
    frames = []
    status_frames = []
    request_summary = {}
    for symbol in symbols:
        path = source_dir / f"{symbol}.csv"
        frame = validate_prices(read_csv(path, symbol))
        first = frame["date"].iloc[0]
        expected = sessions[sessions >= first]
        if not expected.equals(pd.Index(frame["date"])):
            missing = expected.difference(pd.Index(frame["date"]))
            extra = pd.Index(frame["date"]).difference(expected)
            raise ValueError(f"Calendar mismatch {symbol}: missing={list(missing)}, extra={list(extra)}")
        if len(frame) < 120 or audits[symbol]["rows"] != len(frame):
            raise ValueError(f"History or provider row count mismatch: {symbol}")
        frames.append(frame)
        status_frames.append(pd.DataFrame({
            "date": frame["date"], "symbol": symbol, "status": "trading",
            "can_buy": True, "can_sell": True,
        }))
        source_records.append(record(path, frame))
        symbol_requests = [item for item in requests if item["symbol"] == symbol]
        if not symbol_requests or any(item["status"] != 200 or item["provider_code"] != 0 for item in symbol_requests):
            raise ValueError(f"Missing or failed Tencent request: {symbol}")
        request_summary[symbol] = {
            "requested_adjustment": "qfq",
            "returned_keys": sorted({item["returned_key"] for item in symbol_requests}),
            "response_count": len(symbol_requests),
        }

    index_requests = [item for item in requests if item["symbol"] == "000300"]
    if not index_requests or any(item["status"] != 200 or item["returned_key"] != "day" for item in index_requests):
        raise ValueError("Benchmark request record differs")
    if audits["000300"]["rows"] != len(benchmark_source):
        raise ValueError("Benchmark provider row count differs")
    prices = pd.concat(frames, ignore_index=True)
    data_start = prices["date"].min()
    benchmark = benchmark.loc[benchmark["date"] >= data_start].reset_index(drop=True)
    instruments["listing_date"] = prices.groupby("symbol")["date"].min().reindex(symbols).to_numpy()
    manifest = DatasetManifest(
        source="Tencent Securities newfqkline, frozen 2026-10-01 request responses and normalized CSVs",
        is_demo=False, price_mode="adjusted_research", currency="CNY",
        calendar_start=sessions[0].date(), calendar_end=CUTOFF.date(),
        data_start=data_start.date(), data_end=CUTOFF.date(),
        volume_unit="hands (100 shares)", amount_unit="CNY",
        event_coverage="unknown", benchmark_type="price_index",
        field_semantics={
            "etf_ohlc": "Tencent daily ETF prices; qfq requested, provider returned day or qfqday by response; use normalized full-history series as one research price basis",
            "volume": "Tencent numeric volume in hands, independently spot-checked against Sina and within-bar turnover prices",
            "amount": "Tencent numeric amount multiplied by 10,000 into CNY; independent spot checks cover selected observations",
            "benchmark_turnover": "CSI 300 index feed amount in CNY; index price benchmark, not ETF market turnover",
        },
        universe_rule={"min_history_days": 120, "min_avg_turnover_20d": 0},
        limitations=[
            "Qfq was requested, but Tencent returned day for some responses and qfqday for others; adjusted-price semantics are not independently confirmed for every observation.",
            "Observed historical changes in 159995 and 515880 are consistent with 2:1 splits, and 512400 with a CNY 0.08 distribution; these events are not a complete corporate-action ledger.",
            "The selected historical universe has survivorship and availability-sample bias; listing dates are first observations.",
            "Lot size 100, T+1 settlement, and trading eligibility on every observed quote session are research assumptions.",
            "No prices or trading statuses are imputed; unknown gaps cause preparation to fail.",
            "Price limits, market impact, and execution slippage are not modeled by this dataset.",
        ],
    )
    events = pd.DataFrame(columns=["symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"])
    bundle = validate_dataset(DatasetBundle(
        manifest, prices, benchmark, pd.DataFrame({"date": sessions}), instruments,
        pd.concat(status_frames, ignore_index=True), events, pd.DataFrame(),
    ))

    # Audit the prior frozen result; never copy into a supplier-specific cache or regenerate it.
    dataset_path = OUTPUT / "dataset"
    if not dataset_path.exists():
        raise FileNotFoundError("legacy verification requires the existing frozen dataset")
    saved = load_dataset(dataset_path)
    if saved.manifest.model_dump(mode="json") != manifest.model_dump(mode="json"):
        raise ValueError("Saved manifest differs")
    for name in TABLES:
        pd.testing.assert_frame_equal(getattr(saved, name), getattr(bundle, name), check_dtype=False, check_exact=True)

    source_audit = {
        "research_cutoff": CUTOFF.date().isoformat(),
        "selected_symbols": symbols,
        "selected_count": len(symbols),
        "price_rows": len(bundle.prices),
        "benchmark_rows": len(bundle.benchmark),
        "calendar_rows": len(bundle.calendar),
        "data_start": data_start.date().isoformat(),
        "source_files": source_records,
        "provenance": [
            {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(path)}
            for path in (selection_path, instruments_path, requests_path, provider_audit_path, unit_check_path, adjustment_check_path)
        ],
        "tencent_requests_by_symbol": request_summary,
        "benchmark_returned_keys": sorted({item["returned_key"] for item in index_requests}),
        "unit_check": "27/27 sampled ETF observations passed; not full-history independent verification",
        "coverage_check": "Each ETF has exactly every observed benchmark session from its first quote through 2026-09-30; no gap filling or suspension inference",
        "validation": "validate_dataset and exact Parquet roundtrip passed",
        "limitations": manifest.limitations,
    }
    print(json.dumps({
        "symbols": len(symbols), "price_rows": len(bundle.prices),
        "benchmark_rows": len(bundle.benchmark), "calendar_rows": len(bundle.calendar),
        "data_end": CUTOFF.date().isoformat(), "dataset": str(dataset_path), "validation": "passed",
        "audit": source_audit,
    }, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description="Read prepared market snapshots, or audit an existing round-two freeze")
    parser.add_argument("--audit-legacy", action="store_true")
    parser.add_argument("--snapshot")
    parser.add_argument("--requirement", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assumptions", type=Path)
    args = parser.parse_args()
    if args.audit_legacy:
        verify_legacy()
        return
    if any(value is None for value in (args.snapshot, args.requirement, args.output)):
        parser.error("--snapshot, --requirement and --output are required")
    sys.path.insert(0, str(ROOT))
    from scripts.prepare_strategy_research_data import prepare_from_snapshot
    from quanto.data import get_market_root
    assumptions = json.loads(args.assumptions.read_text(encoding="utf-8")) if args.assumptions else {}
    result = prepare_from_snapshot(get_market_root(), args.snapshot, json.loads(args.requirement.read_text(encoding="utf-8")), args.output, assumptions)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
