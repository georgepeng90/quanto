from __future__ import annotations

import pandas as pd


def classify_market_regime(benchmark: pd.DataFrame, config: dict) -> pd.DataFrame:
    ma_window = int(config.get("ma_window", 200))
    tradable_turnover = float(config.get("tradable_turnover", 850_000_000_000))
    danger_turnover = float(config.get("danger_turnover", 700_000_000_000))
    cautious_weight = float(config.get("cautious_total_weight", 0.30))

    out = benchmark.sort_values("date").copy()
    if "turnover" not in out.columns and "amount" in out.columns:
        out = out.rename(columns={"amount": "turnover"})
    out["ma"] = out["close"].rolling(ma_window, min_periods=ma_window).mean()
    above_ma = out["close"] > out["ma"]
    tradable = above_ma & (out["turnover"] > tradable_turnover)
    danger = (~above_ma) & (out["turnover"] < danger_turnover)

    out["regime"] = "cautious"
    out.loc[tradable, "regime"] = "tradable"
    out.loc[danger, "regime"] = "danger"
    out["total_weight_cap"] = cautious_weight
    out.loc[out["regime"] == "tradable", "total_weight_cap"] = 1.0
    out.loc[out["regime"] == "danger", "total_weight_cap"] = 0.0
    return out[["date", "regime", "total_weight_cap"]]


def add_market_regime(benchmark: pd.DataFrame, cautious_total_weight: float = 0.30) -> pd.DataFrame:
    return classify_market_regime(benchmark, {"cautious_total_weight": cautious_total_weight})
