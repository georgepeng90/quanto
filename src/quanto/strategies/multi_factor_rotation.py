from __future__ import annotations

import pandas as pd

from quanto.factors import compute_factor_panel, score_factors


def friday_signal_dates(prices: pd.DataFrame) -> list[pd.Timestamp]:
    dates = pd.Series(pd.to_datetime(prices["date"].unique())).sort_values()
    fridays = dates[dates.dt.weekday == 4]
    if len(fridays) == 0:
        weekly = pd.DataFrame({"date": dates})
        fridays = weekly.groupby(weekly["date"].dt.to_period("W-FRI"))["date"].max()
        return [pd.Timestamp(date) for date in fridays]
    return [pd.Timestamp(date) for date in fridays]


def next_trade_date(prices: pd.DataFrame, signal_date: pd.Timestamp) -> pd.Timestamp | None:
    dates = pd.Series(pd.to_datetime(prices["date"].unique())).sort_values()
    future = dates[dates > signal_date]
    if future.empty:
        return None
    return pd.Timestamp(future.iloc[0])


def generate_rebalance_signals(
    prices: pd.DataFrame,
    cfg: dict,
) -> pd.DataFrame:
    top_n = int(cfg["strategy"]["top_n"])
    factor_data = score_factors(compute_factor_panel(prices, cfg["factors"]), cfg["factors"])
    rows: list[dict] = []
    for signal_date in friday_signal_dates(prices):
        trade_date = next_trade_date(prices, signal_date)
        if trade_date is None:
            continue
        chosen = factor_data[
            (factor_data["signal_date"] == signal_date) & (factor_data["rank"] <= top_n)
        ].sort_values("rank")
        if chosen.empty:
            continue
        strategy_weight = 1 / len(chosen)
        for row in chosen.itertuples():
            rows.append(
                {
                    "signal_date": signal_date,
                    "trade_date": trade_date,
                    "symbol": str(row.symbol),
                    "rank": int(row.rank),
                    "score": float(row.score),
                    "strategy_weight": strategy_weight,
                }
            )
    return pd.DataFrame(rows)
