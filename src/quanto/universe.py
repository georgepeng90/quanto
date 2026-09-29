from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import load_yaml


@dataclass(frozen=True)
class Universe:
    symbols: list[str]
    min_history_days: int = 120
    min_avg_turnover_20d: float = 50_000_000


def load_universe(path: str | Path) -> Universe:
    raw = load_yaml(path)
    settings = raw.get("universe", {})
    symbols = [
        str(item["etf"])
        for item in raw.get("symbols", [])
        if item.get("enabled", True) and item.get("etf")
    ]
    if not symbols:
        raise ValueError("Universe contains no enabled ETF symbols.")
    return Universe(
        symbols=symbols,
        min_history_days=int(settings.get("min_history_days", 120)),
        min_avg_turnover_20d=float(settings.get("min_avg_turnover_20d", 50_000_000)),
    )


def build_eligibility(prices: pd.DataFrame, instruments: pd.DataFrame, rule: dict) -> pd.DataFrame:
    allowed = {"min_history_days", "min_avg_turnover_20d"}
    if set(rule) - allowed:
        raise ValueError(f"Unknown universe rule fields: {sorted(set(rule) - allowed)}")
    min_history = rule.get("min_history_days", 1)
    min_turnover = rule.get("min_avg_turnover_20d", 0)
    if not isinstance(min_history, int) or min_history < 1 or not isinstance(min_turnover, (int, float)) or min_turnover < 0:
        raise ValueError("Invalid universe eligibility rule")
    ordered = prices.sort_values(["symbol", "date"]).copy()
    counts = ordered.groupby("symbol").cumcount() + 1
    avg_amount = ordered.groupby("symbol")["amount"].transform(lambda values: values.rolling(20, min_periods=1).mean())
    listing = instruments.set_index("symbol")["listing_date"]
    listing_dates = pd.to_datetime(ordered["symbol"].map(listing))
    if listing_dates.isna().any():
        raise ValueError("Prices include unregistered symbols")
    ordered["eligible"] = ((counts >= min_history) & (avg_amount >= min_turnover) & (ordered["date"] >= listing_dates)).astype(bool)
    return ordered[["date", "symbol", "eligible"]].sort_values(["date", "symbol"]).reset_index(drop=True)
