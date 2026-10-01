from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data" / "raw"
UNIVERSE_PATH = PROJECT_ROOT / "configs" / "universe.yaml"
RESEARCH_CUTOFF = pd.Timestamp("2026-05-19")
MIN_HISTORY_DAYS = 120


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_market_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"symbol": str})
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    return frame


def input_record(path: Path, frame: pd.DataFrame, date_column: str = "date") -> dict:
    dates = frame[date_column]
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "sha256": sha256(path),
        "row_count": int(len(frame)),
        "coverage_start": dates.min().date().isoformat() if not frame.empty else None,
        "coverage_end": dates.max().date().isoformat() if not frame.empty else None,
        "duplicate_dates": int(dates.duplicated().sum()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a frozen, audited ETF dataset for strategy research."
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Research root directory; writes dataset/ and source_audit.json.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from quanto.backtest.dataset_loaders import load_dataset, validate_dataset
    from quanto.backtest.dataset_schemas import DatasetBundle, DatasetManifest
    from quanto.backtest.dataset_storage import write_parquet_dataset

    config = yaml.safe_load(UNIVERSE_PATH.read_text(encoding="utf-8"))
    configured = {
        str(item["etf"]): item
        for item in config.get("symbols", [])
        if item.get("enabled", True) and item.get("etf")
    }

    benchmark_path = DATA_ROOT / "benchmark" / "sh000300.csv"
    benchmark_source = read_market_csv(benchmark_path)
    benchmark_source = benchmark_source.sort_values("date").reset_index(drop=True)
    benchmark_dates = list(benchmark_source["date"])
    benchmark_calendar = set(benchmark_dates)
    if benchmark_source["date"].duplicated().any():
        raise ValueError(f"Duplicate benchmark dates in {benchmark_path}")
    if RESEARCH_CUTOFF not in benchmark_calendar:
        raise ValueError(f"Research cutoff {RESEARCH_CUTOFF.date()} is absent from benchmark")

    benchmark = benchmark_source.loc[benchmark_source["date"] <= RESEARCH_CUTOFF].copy()
    if not benchmark.empty and benchmark["date"].iloc[-1] != RESEARCH_CUTOFF:
        raise ValueError("Benchmark has no observation on the requested research cutoff")

    input_records = [input_record(benchmark_path, benchmark_source)]
    etf_files = sorted((DATA_ROOT / "etf").glob("*.csv"))
    source_frames: dict[str, pd.DataFrame] = {}
    source_coverage: dict[str, dict] = {}
    gap_sessions: dict[str, list[str]] = {}
    for path in etf_files:
        symbol = path.stem
        frame = read_market_csv(path).sort_values("date").reset_index(drop=True)
        if "symbol" not in frame or not frame["symbol"].astype(str).eq(symbol).all():
            raise ValueError(f"ETF symbol column does not match filename: {path}")
        if frame["date"].duplicated().any():
            raise ValueError(f"Duplicate ETF dates in {path}")
        source_frames[symbol] = frame
        input_records.append(input_record(path, frame))
        visible = frame.loc[frame["date"] <= RESEARCH_CUTOFF]
        start = visible["date"].min() if not visible.empty else None
        end = visible["date"].max() if not visible.empty else None
        missing = []
        if start is not None:
            observed_sessions = set(visible["date"])
            missing = [
                day.date().isoformat()
                for day in benchmark_dates
                if start <= day <= RESEARCH_CUTOFF and day not in observed_sessions
            ]
        gap_sessions[symbol] = missing
        source_coverage[symbol] = {
            "coverage_start": frame["date"].min().date().isoformat() if not frame.empty else None,
            "coverage_end": frame["date"].max().date().isoformat() if not frame.empty else None,
            "through_cutoff_end": end.date().isoformat() if end is not None else None,
            "rows_through_cutoff": int(len(visible)),
            "missing_benchmark_sessions_through_cutoff": missing,
        }

    input_records.append({
        "path": str(UNIVERSE_PATH.relative_to(PROJECT_ROOT)),
        "sha256": sha256(UNIVERSE_PATH),
        "row_count": len(configured),
        "coverage_start": None,
        "coverage_end": None,
        "duplicate_dates": 0,
    })

    exclusions: dict[str, list[str]] = {}
    selected: list[str] = []
    missing_files: list[str] = []
    for symbol, item in configured.items():
        reasons = []
        frame = source_frames.get(symbol)
        if frame is None:
            reasons.append("no_source_csv")
            missing_files.append(symbol)
        else:
            coverage = source_coverage[symbol]
            if coverage["through_cutoff_end"] != RESEARCH_CUTOFF.date().isoformat():
                reasons.append("does_not_reach_research_cutoff")
            if gap_sessions[symbol]:
                reasons.append("unknown_missing_benchmark_sessions")
            if coverage["rows_through_cutoff"] < MIN_HISTORY_DAYS:
                reasons.append("fewer_than_120_observations")
        if reasons:
            exclusions[symbol] = reasons
        else:
            selected.append(symbol)

    raw_symbols_without_config = sorted(set(source_frames) - set(configured))
    for symbol in raw_symbols_without_config:
        exclusions[symbol] = ["not_enabled_in_universe_config"]
        if source_coverage[symbol]["through_cutoff_end"] != RESEARCH_CUTOFF.date().isoformat():
            exclusions[symbol].append("does_not_reach_research_cutoff")
        if gap_sessions[symbol]:
            exclusions[symbol].append("unknown_missing_benchmark_sessions")

    if not selected:
        raise ValueError("No configured ETF passes coverage checks")

    price_frames = []
    instrument_rows = []
    status_frames = []
    for symbol in selected:
        frame = source_frames[symbol].loc[
            source_frames[symbol]["date"] <= RESEARCH_CUTOFF
        ].copy()
        price_frames.append(frame)
        first_observed = frame["date"].min()
        instrument_rows.append({
            "symbol": symbol,
            "lot_size": 100,
            "settlement_sessions": 1,
            "listing_date": first_observed,
            "group": str(configured[symbol].get("category") or "unknown"),
        })
        active_days = [day for day in benchmark_dates if first_observed <= day <= RESEARCH_CUTOFF]
        status_frames.append(pd.DataFrame({
            "date": active_days,
            "symbol": symbol,
            "status": "trading",
            "can_buy": True,
            "can_sell": True,
        }))

    prices = pd.concat(price_frames, ignore_index=True)
    data_start = prices["date"].min().date()
    manifest = DatasetManifest(
        source=(
            "Local raw CSV cache; ETF source/adjustment flags are absent; qfq is inferred "
            "from the repository's historical AKShare fetcher request"
        ),
        is_demo=False,
        price_mode="adjusted_research",
        currency="CNY",
        calendar_start=benchmark_dates[0].date(),
        calendar_end=benchmark_dates[-1].date(),
        data_start=data_start,
        data_end=RESEARCH_CUTOFF.date(),
        volume_unit="unverified",
        amount_unit="unverified",
        event_coverage="unknown",
        benchmark_type="price_index",
        field_semantics={
            "close": "ETF close price; source adjustment metadata unavailable",
            "amount": "raw CSV amount; unit unverified",
            "benchmark_turnover": "selected benchmark feed turnover; not market-wide turnover",
        },
        universe_rule={"min_history_days": MIN_HISTORY_DAYS, "min_avg_turnover_20d": 0},
        limitations=[
            "The source CSVs lack independent source and price-adjustment evidence; qfq is inferred from the repository's historical fetcher request.",
            "Universe constituents and available history create survivorship and availability-sample bias.",
            "Listing dates use first observation and may not be actual listing dates; lot size 100, T+1 settlement, and all observed sessions tradable are research assumptions.",
            "Price limits, market impact, and execution slippage are not modeled by this dataset.",
            "Unknown quote gaps are excluded as symbols; they are not imputed or labeled as suspensions.",
            "The benchmark calendar retains observed sessions through 2026-05-21; price and benchmark performance data end on 2026-05-19.",
        ],
    )
    events = pd.DataFrame(columns=[
        "symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"
    ])
    bundle = DatasetBundle(
        manifest=manifest,
        prices=prices,
        benchmark=benchmark,
        calendar=pd.DataFrame({"date": benchmark_dates}),
        instruments=pd.DataFrame(instrument_rows),
        status=pd.concat(status_frames, ignore_index=True),
        events=events,
        eligibility=pd.DataFrame(),
    )
    validated = validate_dataset(bundle)

    output_root = args.output.resolve()
    dataset_path = output_root / "dataset"
    output_root.mkdir(parents=True, exist_ok=True)
    if dataset_path.exists() and any(dataset_path.iterdir()):
        existing = load_dataset(dataset_path)
        if existing.manifest.model_dump(mode="json") != manifest.model_dump(mode="json"):
            raise ValueError(f"Existing dataset manifest differs from source inputs: {dataset_path}")
        for name in ("prices", "benchmark", "calendar", "instruments", "status", "events", "eligibility"):
            pd.testing.assert_frame_equal(
                getattr(existing, name).reset_index(drop=True),
                getattr(validated, name).reset_index(drop=True),
                check_dtype=False,
                check_exact=True,
            )
    else:
        write_parquet_dataset(validated, dataset_path)

    audit = {
        "research_cutoff": RESEARCH_CUTOFF.date().isoformat(),
        "calendar_observation_end": benchmark_dates[-1].date().isoformat(),
        "data_start": data_start.isoformat(),
        "configured_enabled_symbols": len(configured),
        "raw_etf_csv_count": len(etf_files),
        "selected_symbols": selected,
        "selected_count": len(selected),
        "selected_price_rows": int(len(validated.prices)),
        "benchmark_rows_through_cutoff": int(len(validated.benchmark)),
        "calendar_rows_including_observed_future_sessions": int(len(validated.calendar)),
        "eligibility_rows": int(len(validated.eligibility)),
        "inputs": input_records,
        "etf_source_coverage": source_coverage,
        "exclusions": exclusions,
        "missing_session_exclusions": {
            symbol: {
                "reason": "unknown_missing_benchmark_sessions; excluded without imputing prices or assuming suspension",
                "dates": missing,
            }
            for symbol, missing in gap_sessions.items()
            if missing
        },
        "configured_symbols_without_source_files": missing_files,
        "validation": "passed",
        "limitations": manifest.limitations,
    }
    (output_root / "source_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output_root": str(output_root),
        "dataset_path": str(dataset_path),
        "source_audit": str(output_root / "source_audit.json"),
        "selected_count": len(selected),
        "selected_symbols": selected,
        "excluded_symbols": len(exclusions),
        "price_rows": int(len(validated.prices)),
        "benchmark_rows": int(len(validated.benchmark)),
        "calendar_rows": int(len(validated.calendar)),
        "validation": "passed",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
