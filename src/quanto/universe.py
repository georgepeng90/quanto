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


def apply_universe_filters(prices: pd.DataFrame, universe: Universe) -> pd.DataFrame:
    prices = prices[prices["symbol"].astype(str).isin(universe.symbols)].copy()
    prices["symbol"] = prices["symbol"].astype(str)
    prices = prices.sort_values(["symbol", "date"])
    counts = prices.groupby("symbol").cumcount() + 1
    avg_amount = (
        prices.groupby("symbol")["amount"]
        .transform(lambda s: s.rolling(20, min_periods=1).mean())
        .fillna(0)
    )
    return prices[
        (counts >= universe.min_history_days)
        & (avg_amount >= universe.min_avg_turnover_20d)
    ].copy()
