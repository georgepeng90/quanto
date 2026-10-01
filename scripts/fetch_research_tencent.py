"""Fetch a fixed ETF pool from Tencent qfq K-lines with raw-response audit."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import time

import pandas as pd
import requests


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "outputs/tencent-replay-20261001"
RAW = ROOT / "tencent_raw"
NORMALIZED = ROOT / "tencent_qfq"
SYMBOLS = json.loads((PROJECT / "docs/research/2026-10-01/source_audit.json").read_text(encoding="utf-8"))["selected_symbols"]
BENCHMARK = "000300"
URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
YEARS = tuple(range(2010, 2027, 2))
END = "2026-09-30"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
FIELDS = ["date", "symbol", "open", "high", "low", "close", "volume", "amount"]


def fetch_window(symbol: str, year: int) -> tuple[dict, list[dict]]:
    market_symbol = ("sz" if symbol.startswith("1") else "sh") + symbol
    params = {
        "_var": f"kline_dayqfq{year}",
        "param": f"{market_symbol},day,{year}-01-01,{year + 1}-12-31,640,qfq",
        "r": "0.8205512681390605",
    }
    metadata = {"symbol": symbol, "market_symbol": market_symbol,
                "year": year, "url": URL, "params": params}
    for attempt in range(3):
        try:
            response = requests.get(URL, params=params, headers=HEADERS, timeout=25)
            metadata.update({"received_utc": datetime.now(timezone.utc).isoformat(),
                             "status": response.status_code, "final_url": response.url,
                             "bytes": len(response.content),
                             "content_type": response.headers.get("Content-Type")})
            raw_path = RAW / f"{market_symbol}_{year}.txt"
            raw_path.write_bytes(response.content)
            metadata["raw_path"] = str(raw_path.relative_to(ROOT))
            response.raise_for_status()
            parsed = json.loads(response.text.split("=", 1)[1])
            metadata["provider_code"] = parsed.get("code")
            payload = parsed.get("data", {}).get(market_symbol, {})
            key = "qfqday" if "qfqday" in payload else "day"
            raw_rows = payload.get(key)
            if raw_rows is None:
                raise ValueError(f"missing {key}; provider keys={list(payload)}")
            if not isinstance(raw_rows, list):
                raise ValueError(f"invalid {key} type")
            metadata["rows"] = len(raw_rows)
            metadata["returned_key"] = key
            metadata["first_date"] = raw_rows[0][0] if raw_rows else None
            metadata["last_date"] = raw_rows[-1][0] if raw_rows else None
            rows = []
            for raw in raw_rows:
                if len(raw) < 9:
                    raise ValueError(f"short raw row: {raw}")
                rows.append({"date": raw[0], "symbol": symbol,
                             "open": float(raw[1]), "high": float(raw[3]),
                             "low": float(raw[4]), "close": float(raw[2]),
                             "volume": float(raw[5]),
                             "amount": round(float(raw[8]) * 10000, 2)})
            metadata.pop("error", None)
            return metadata, rows
        except Exception as exc:
            metadata["error"] = repr(exc)
            if attempt < 2:
                time.sleep(1 + attempt)
    return metadata, []


def check_symbol(symbol: str, windows: list[tuple[dict, list[dict]]], benchmark_dates: set[str] | None) -> dict:
    records = []
    for metadata, rows in windows:
        records.extend(rows)
    frame = pd.DataFrame(records, columns=FIELDS)
    result = {"symbol": symbol, "window_count": len(windows),
              "failed_windows": [item[0]["year"] for item in windows if "error" in item[0]],
              "raw_rows": len(frame)}
    if frame.empty:
        result["error"] = "no rows"
        return result
    frame = frame.loc[frame.date.between("2010-01-01", END)].copy()
    duplicates = frame.loc[frame.duplicated("date", keep=False)]
    conflict_dates = []
    for day, group in duplicates.groupby("date"):
        if len(group[FIELDS[2:]].drop_duplicates()) > 1:
            conflict_dates.append(day)
    result["duplicate_conflict_dates"] = conflict_dates
    if conflict_dates:
        result["error"] = "overlapping windows disagree; merged CSV rejected"
        return result
    frame = frame.drop_duplicates("date", keep="last").sort_values("date")
    numeric = frame[FIELDS[2:]]
    invalid = (~(numeric.notna().all(axis=1) & (numeric >= 0).all(axis=1)
                 & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
                 & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
                 & (frame[["open", "high", "low", "close"]] > 0).all(axis=1)))
    result["invalid_ohlc_dates"] = frame.loc[invalid, "date"].tolist()
    result["rows"] = len(frame)
    result["first_date"] = frame.date.iloc[0]
    result["last_date"] = frame.date.iloc[-1]
    result["new_rows_after_cache"] = None
    if symbol != BENCHMARK:
        old = pd.read_csv(PROJECT / "data/raw/etf" / f"{symbol}.csv")
        joined = frame.merge(old, on="date", suffixes=("_new", "_old"))
        result["old_overlap_rows"] = len(joined)
        result["old_overlap_close_max_abs_difference"] = float((joined.close_new - joined.close_old).abs().max()) if len(joined) else None
        result["old_overlap_close_mismatch_gt_0p001"] = int(((joined.close_new - joined.close_old).abs() > .00100001).sum())
        result["old_overlap_recent_mismatch_gt_0p001"] = int(((joined.loc[joined.date >= "2024-02-07", "close_new"] - joined.loc[joined.date >= "2024-02-07", "close_old"]).abs() > .00100001).sum())
        result["new_rows_after_cache"] = int((frame.date > old.date.max()).sum())
    else:
        old = pd.read_csv(PROJECT / "data/raw/benchmark/sh000300.csv")
        joined = frame.merge(old, on="date", suffixes=("_new", "_old"))
        result["old_overlap_rows"] = len(joined)
        result["old_overlap_close_max_abs_difference"] = float((joined.close_new - joined.close_old).abs().max()) if len(joined) else None
        result["new_rows_after_cache"] = int((frame.date > old.date.max()).sum())
    if benchmark_dates is not None:
        result["missing_benchmark_sessions_since_first_date"] = sorted(d for d in benchmark_dates if frame.date.iloc[0] <= d <= END and d not in set(frame.date))
    output = NORMALIZED / ("benchmark_sh000300.csv" if symbol == BENCHMARK else f"{symbol}.csv")
    frame.to_csv(output, index=False, columns=FIELDS, float_format="%.4f", lineterminator="\n")
    result["normalized_path"] = str(output.relative_to(ROOT))
    return result


def main() -> None:
    global ROOT, RAW, NORMALIZED, YEARS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New empty output directory")
    parser.add_argument("--latest-only", action="store_true")
    args = parser.parse_args()
    ROOT = args.output.resolve()
    if ROOT.exists() and any(ROOT.iterdir()):
        raise ValueError("Use a new empty output directory; do not overwrite frozen market data")
    RAW, NORMALIZED = ROOT / "tencent_raw", ROOT / "tencent_qfq"
    RAW.mkdir(parents=True, exist_ok=True)
    NORMALIZED.mkdir(parents=True, exist_ok=True)
    YEARS = (2026,) if args.latest_only else tuple(range(2010, 2027, 2))
    inputs = [(symbol, year) for symbol in [BENCHMARK, *SYMBOLS] for year in YEARS]
    results = {symbol: [] for symbol in [BENCHMARK, *SYMBOLS]}
    metadata = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_window, symbol, year): (symbol, year) for symbol, year in inputs}
        for future in as_completed(futures):
            symbol, year = futures[future]
            item = future.result()
            results[symbol].append(item)
            metadata.append(item[0])
            print(symbol, year, item[0].get("rows", 0), item[0].get("last_date"), item[0].get("error", ""), flush=True)
    metadata.sort(key=lambda item: (item["symbol"], item["year"]))
    (ROOT / "tencent_requests.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    benchmark_audit = check_symbol(BENCHMARK, results[BENCHMARK], None)
    bench_path = NORMALIZED / "benchmark_sh000300.csv"
    benchmark_dates = set(pd.read_csv(bench_path).date) if bench_path.exists() else set()
    audits = [benchmark_audit] + [check_symbol(symbol, results[symbol], benchmark_dates) for symbol in SYMBOLS]
    summary = {
        "source": "Tencent Securities newfqkline GET via direct requests; schema interpreted from installed AKShare stock_zh_a_hist_tx",
        "price_adjustment": "qfq requested for ETFs; actual returned_key is recorded per response and may be day or qfqday; benchmark day is price index",
        "volume_unit": "source raw numeric, matches existing Eastmoney cache volume unit (hands)",
        "amount_unit": "source raw amount in 10,000 CNY, multiplied by 10,000 to CNY",
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "requested_symbols": SYMBOLS,
        "benchmark": BENCHMARK,
        "years": YEARS,
        "audits": audits,
    }
    (ROOT / "tencent_audit.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("AUDIT", json.dumps({"requested": len(SYMBOLS), "written": sum("normalized_path" in item for item in audits[1:]),
                               "ends_at_cutoff": sum(item.get("last_date") == END for item in audits[1:]),
                               "any_window_failures": sum(bool(item["failed_windows"]) for item in audits),
                               "any_duplicate_conflicts": sum(bool(item.get("duplicate_conflict_dates")) for item in audits),
                               "any_invalid_ohlc": sum(bool(item.get("invalid_ohlc_dates")) for item in audits)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
