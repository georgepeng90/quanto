"""Reconcile the round-three winner's extreme days with independent Sina bars.

Uses saved public responses so a rerun never changes the frozen research input.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs/strategy-research-20261002-round3/runs/full__breadth_timing_v1__cost1"
DATA = ROOT / "outputs/etf-expansion-20261002/shared-cache/dataset"
RAW = ROOT / "outputs/strategy-research-20261002-round3/winner-price-audit"
REPORT = ROOT / "docs/research/2026-10-02-round3/winner-price-audit.json"
SYMBOLS = ("sz159363", "sh515880", "sh588200", "sz159732", "sh512400",
           "sz159851", "sz159869", "sz159755", "sh562500", "sh512200")
EXTRA_DATES = ("2026-03-03", "2026-05-21", "2026-05-22", "2026-05-25")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rounded(value: float, digits: int = 8) -> float:
    return round(float(value), digits)


def main() -> None:
    curves = pd.read_csv(RUN / "curves.csv")
    fills = pd.read_csv(RUN / "fills.csv", dtype={"symbol": str})
    return_by_date = curves.set_index("date")["return"]
    prices = pd.read_parquet(DATA / "prices.parquet")
    prices["date"] = prices.date.dt.strftime("%Y-%m-%d")
    price = prices.set_index(["date", "symbol"])
    high_days = curves.nlargest(5, "return").date.tolist()
    low_days = curves.nsmallest(5, "return").date.tolist()
    selected = set(high_days + low_days + list(EXTRA_DATES))

    # Mark-to-market the previous inventory, then value each fill at the close.
    # This also makes commissions and execution price differences explicit.
    inventory: dict[str, int] = {}
    previous_price_date: dict[str, str] = {}
    fills_by_day = dict(tuple(fills.groupby("trade_date", sort=False)))
    days = []
    largest_reconciliation_error = 0.0
    prior_equity = None
    for row in curves.itertuples():
        contributions: dict[str, float] = {}
        for symbol, quantity in inventory.items():
            if quantity and symbol in previous_price_date and (row.date, symbol) in price.index:
                earlier = previous_price_date[symbol]
                contributions[symbol] = quantity * (price.loc[(row.date, symbol), "close"] - price.loc[(earlier, symbol), "close"])
        for fill in fills_by_day.get(row.date, pd.DataFrame()).itertuples():
            quantity = int(fill.quantity) * (1 if fill.side == "buy" else -1)
            close = price.loc[(row.date, fill.symbol), "close"]
            contributions[fill.symbol] = contributions.get(fill.symbol, 0.0) + quantity * (close - fill.price) - fill.commission
            inventory[fill.symbol] = inventory.get(fill.symbol, 0) + quantity
        for symbol, quantity in inventory.items():
            if quantity and (row.date, symbol) in price.index:
                previous_price_date[symbol] = row.date
        change = row.equity - prior_equity if prior_equity is not None else 0.0
        error = abs(change - sum(contributions.values()))
        largest_reconciliation_error = max(largest_reconciliation_error, error)
        if row.date in selected:
            top = sorted(contributions.items(), key=lambda item: abs(item[1]), reverse=True)[:5]
            days.append({"date": row.date, "portfolio_return": rounded(return_by_date.loc[row.date]),
                         "equity_change_cny": rounded(change, 2),
                         "reconciliation_error_cny": rounded(error, 6),
                         "largest_contributors": [{"symbol": symbol, "pnl_cny": rounded(value, 2),
                                                   "prior_equity_contribution": rounded(value / prior_equity)}
                                                  for symbol, value in top]})
        prior_equity = row.equity
    assert largest_reconciliation_error < 1e-5, largest_reconciliation_error

    comparisons = []
    sources = []
    for source_symbol in SYMBOLS:
        source_path = RAW / f"sina-{source_symbol}-daily.json"
        raw = json.loads(source_path.read_bytes())
        source = pd.DataFrame(raw)
        source["date"] = source.day.str[:10]
        source["close"] = pd.to_numeric(source.close)
        symbol = source_symbol[2:]
        frozen = prices[prices.symbol == symbol][["date", "open", "high", "low", "close"]]
        merged = frozen.merge(source[["date", "open", "high", "low", "close"]], on="date", suffixes=("_frozen", "_sina"))
        merged["close_sina"] = pd.to_numeric(merged.close_sina)
        merged["frozen_return"] = merged.close_frozen.pct_change()
        merged["sina_return"] = merged.close_sina.pct_change()
        merged["close_ratio"] = merged.close_frozen / merged.close_sina
        sources.append({"symbol": symbol, "url": "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
                        f"?symbol={source_symbol}&scale=240&ma=no&datalen=1500",
                        "http_status": 200, "raw_file": source_path.relative_to(ROOT).as_posix(),
                        "sha256": digest(source_path), "raw_rows": len(raw),
                        "raw_first_date": source.date.iloc[0], "raw_last_date": source.date.iloc[-1],
                        "frozen_overlap_rows": len(merged)})
        for checked in merged[merged.date.isin(selected)].itertuples():
            comparisons.append({"date": checked.date, "symbol": symbol,
                                "frozen_close": rounded(checked.close_frozen, 6),
                                "sina_close": rounded(checked.close_sina, 6),
                                "frozen_return": rounded(checked.frozen_return),
                                "sina_return": rounded(checked.sina_return),
                                "absolute_return_difference": rounded(abs(checked.frozen_return - checked.sina_return)),
                                "close_ratio": rounded(checked.close_ratio, 6),
                                "frozen_ohlc": [rounded(getattr(checked, f"{field}_frozen"), 6) for field in ("open", "high", "low", "close")],
                                "sina_ohlc": [rounded(float(getattr(checked, f"{field}_sina")), 6) for field in ("open", "high", "low", "close")]})
    comparisons.sort(key=lambda row: (row["date"], row["symbol"]))
    comparison_by_key = {(row["date"], row["symbol"]): row for row in comparisons}
    leading_checks = []
    for day in days:
        leader = day["largest_contributors"][0]
        checked = comparison_by_key[(day["date"], leader["symbol"])]
        leading_checks.append({"date": day["date"], "symbol": leader["symbol"],
                               "pnl_cny": leader["pnl_cny"],
                               "frozen_return": checked["frozen_return"],
                               "sina_return": checked["sina_return"],
                               "absolute_return_difference": checked["absolute_return_difference"]})
    comparison_path = RAW / "targeted-comparisons.json"
    comparison_path.write_text(json.dumps(comparisons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    max_difference = max(comparisons, key=lambda row: row["absolute_return_difference"])
    event_rows = len(pd.read_parquet(DATA / "events.parquet"))
    result = {
        "scope": "Full breadth_timing_v1 cost1 winner; five highest and five lowest daily returns plus four March-May 2026 moves",
        "method": "Reconstruct close-to-close inventory P&L plus each buy/sell fill's close-versus-execution difference and commission; compare frozen daily bars with saved Sina scale=240 daily bars. Raw and adjusted levels are not required to match.",
        "input_sha256": {path.relative_to(ROOT).as_posix(): digest(path) for path in
                         (RUN / "curves.csv", RUN / "fills.csv", RUN / "equity.csv", RUN / "average_weights.csv", DATA / "prices.parquet", DATA / "events.parquet")},
        "top_positive_dates": high_days, "top_negative_dates": low_days,
        "additional_march_may_dates": list(EXTRA_DATES), "days": days,
        "pnl_reconciliation": {"all_curve_days_checked": len(curves), "max_absolute_error_cny": rounded(largest_reconciliation_error, 8)},
        "independent_source": "Sina CN_MarketData.getKLineData daily scale=240; downloaded 2026-10-02",
        "source_files": sources,
        "targeted_comparisons": {"rows": len(comparisons), "symbols": len(SYMBOLS), "dates": len(selected),
                                 "file": comparison_path.relative_to(ROOT).as_posix(), "sha256": digest(comparison_path),
                                 "exact_daily_return_matches": sum(row["absolute_return_difference"] == 0 for row in comparisons),
                                 "nonexact_daily_return_matches": sum(row["absolute_return_difference"] != 0 for row in comparisons),
                                 "within_half_percentage_point": sum(row["absolute_return_difference"] <= .005 for row in comparisons),
                                 "max_absolute_daily_return_difference": max_difference["absolute_return_difference"],
                                 "max_difference_case": max_difference,
                                 "over_half_percentage_point_cases": [row for row in comparisons if row["absolute_return_difference"] > .005],
                                 "differences_over_one_percentage_point": sum(row["absolute_return_difference"] > .01 for row in comparisons)},
        "largest_contributor_cross_checks": leading_checks,
        "price_basis_observations": [
            "159363 frozen/Sina close ratio changes from about 1/2 before 2025-07-21 to 1 after; 515880 changes from about 1/6 to 1/2 on 2026-02-03. These are consistent with vendor adjustments around unit changes, not proof of the corporate actions.",
            "588200 is about 1/3 of Sina raw prices through the checked 2026 dates. 159732, 159869, 159755 and 562500 match Sina close levels on checked days.",
            "512400 has an approximately 0.095 CNY additive adjusted/raw gap on 2025-04-07: both series fall 0.105 CNY but percentage returns differ by 0.9888 percentage points. Its level difference should not be mistaken for an erroneous raw trade price."],
        "event_rows": event_rows,
        "conclusion": "The high-impact daily moves, including the April-May 2026 jumps, are corroborated by targeted independent raw-price histories: 93 of 137 daily-return pairs match exactly and 44 differ, with the largest difference 0.9888 percentage points for 512400 on 2025-04-07. Adjusted price scales explain level differences and can change percentage-return denominators. This price-path check does not establish full correctness of all 305 ETFs, historical corporate actions, the adjusted-price execution model, or point-in-time tradability.",
    }
    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(days)} days; {len(comparisons)} source pairs; max return difference {max_difference['absolute_return_difference']:.6%}; P&L error {largest_reconciliation_error:.3g} CNY")


if __name__ == "__main__":
    main()
