from __future__ import annotations

import numpy as np
import pandas as pd


FACTOR_COLUMNS = ["momentum", "low_volatility", "volume_confirmation", "prosperity_delta"]


def active_factor_weights(factor_config: dict) -> dict[str, float]:
    weights: dict[str, float] = {}
    for name in FACTOR_COLUMNS:
        cfg = factor_config.get(name, {})
        if cfg.get("enabled", False):
            weights[name] = float(cfg.get("weight", 0.0))
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("At least one factor must be enabled with a positive weight.")
    if factor_config.get("normalize_active_weights", True):
        weights = {name: weight / total for name, weight in weights.items()}
    return weights


def _zscore(series: pd.Series) -> pd.Series:
    std = series.std(ddof=0)
    if not np.isfinite(std) or std == 0:
        return pd.Series(0.0, index=series.index)
    return (series - series.mean()) / std


def score_factors(factors: pd.DataFrame, factor_config: dict) -> pd.DataFrame:
    weights = active_factor_weights(factor_config)
    needed = ["signal_date", "symbol", *weights.keys()]
    missing = set(needed) - set(factors.columns)
    if missing:
        raise ValueError(f"Factor panel missing columns: {sorted(missing)}")

    panel = factors[needed].dropna(subset=list(weights.keys())).copy()
    if panel.empty:
        return pd.DataFrame(columns=["signal_date", "symbol", "score", "rank", *weights.keys()])

    for factor in weights:
        panel[f"{factor}_z"] = panel.groupby("signal_date")[factor].transform(_zscore)

    panel["score"] = sum(panel[f"{factor}_z"] * weight for factor, weight in weights.items())
    panel["rank"] = panel.groupby("signal_date")["score"].rank(ascending=False, method="first").astype(int)
    return panel.sort_values(["signal_date", "rank"]).reset_index(drop=True)
