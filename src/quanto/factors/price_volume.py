from __future__ import annotations

import pandas as pd


def compute_factor_panel(prices: pd.DataFrame, factor_config: dict) -> pd.DataFrame:
    """Compute trailing factors with only data available at each close."""
    df = prices.sort_values(["symbol", "date"]).copy()
    grouped = df.groupby("symbol", group_keys=False)
    returns = grouped["close"].pct_change()
    df["ret_1d"] = returns

    momentum_cfg = factor_config.get("momentum", {})
    windows = momentum_cfg.get("windows", [20, 60])
    mom_parts = [grouped["close"].pct_change(int(window)) for window in windows]
    df["momentum"] = sum(mom_parts) / len(mom_parts)

    vol_window = int(factor_config.get("low_volatility", {}).get("window", 20))
    df["low_volatility"] = -grouped["ret_1d"].rolling(vol_window).std().reset_index(level=0, drop=True)

    volume_cfg = factor_config.get("volume_confirmation", {})
    short_window = int(volume_cfg.get("short_window", 5))
    long_window = int(volume_cfg.get("long_window", 20))
    short_vol = grouped["volume"].rolling(short_window).mean().reset_index(level=0, drop=True)
    long_vol = grouped["volume"].rolling(long_window).mean().reset_index(level=0, drop=True)
    df["volume_confirmation"] = short_vol / long_vol

    cols = ["date", "symbol", "momentum", "low_volatility", "volume_confirmation"]
    return df[cols].rename(columns={"date": "signal_date"})
