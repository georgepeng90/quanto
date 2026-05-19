from __future__ import annotations

import numpy as np
import pandas as pd


def trailing_volatility(prices: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    df = prices.sort_values(["symbol", "date"]).copy()
    df["ret"] = df.groupby("symbol")["close"].pct_change()
    df["vol_20d"] = df.groupby("symbol")["ret"].rolling(window).std().reset_index(level=0, drop=True)
    return df[["date", "symbol", "vol_20d"]]


def size_positions(
    targets: pd.DataFrame,
    volatilities: pd.DataFrame,
    regime: pd.DataFrame,
    risk_config: dict,
) -> pd.DataFrame:
    if targets.empty:
        return pd.DataFrame(columns=["trade_date", "symbol", "target_weight", "strategy_weight", "regime"])

    max_symbol_weight = float(risk_config.get("max_symbol_weight", 0.15))
    single_risk_limit = float(risk_config.get("single_symbol_risk_limit", 0.005))
    portfolio_var_limit = float(risk_config.get("portfolio_var_limit", 0.02))
    multiplier = float(risk_config.get("var_confidence_multiplier", 2.33))

    risk_date_col = "signal_date" if "signal_date" in targets.columns else "trade_date"
    out = targets.merge(volatilities, left_on=[risk_date_col, "symbol"], right_on=["date", "symbol"], how="left")
    out = out.drop(columns=["date"], errors="ignore")
    out = out.merge(regime, left_on=risk_date_col, right_on="date", how="left").drop(columns=["date"], errors="ignore")
    out["regime"] = out["regime"].fillna("cautious")
    out["total_weight_cap"] = out["total_weight_cap"].fillna(float(risk_config.get("cautious_total_weight", 0.30)))

    vol = out["vol_20d"].replace(0, np.nan)
    risk_weight_cap = single_risk_limit / (vol * multiplier)
    out["target_weight"] = out["strategy_weight"].clip(upper=max_symbol_weight)
    out["target_weight"] = np.minimum(out["target_weight"], risk_weight_cap.fillna(max_symbol_weight))
    out["target_weight"] = out["target_weight"].clip(lower=0, upper=max_symbol_weight)

    def scale_group(group: pd.DataFrame) -> pd.DataFrame:
        cap = float(group["total_weight_cap"].iloc[0])
        total = group["target_weight"].sum()
        if total > cap and total > 0:
            group = group.copy()
            group["target_weight"] = group["target_weight"] * cap / total
        avg_vol = group["vol_20d"].fillna(0).mean()
        estimated_var = group["target_weight"].sum() * avg_vol * multiplier
        if estimated_var > portfolio_var_limit and estimated_var > 0:
            group = group.copy()
            group["target_weight"] *= portfolio_var_limit / estimated_var
        return group

    out = pd.concat([scale_group(group) for _, group in out.groupby("trade_date")], ignore_index=True)
    out.loc[out["regime"] == "danger", "target_weight"] = 0.0
    return out[["trade_date", "symbol", "target_weight", "strategy_weight", "regime"]].reset_index(drop=True)


def scale_target_weights(candidates: pd.DataFrame, total_weight_cap: float, cfg: dict) -> dict[str, float]:
    if candidates.empty or total_weight_cap <= 0:
        return {}
    targets = candidates.copy()
    targets["trade_date"] = pd.Timestamp("2000-01-03")
    targets["strategy_weight"] = targets.get("strategy_weight", 1.0 / len(targets))
    volatilities = pd.DataFrame(
        {
            "date": targets["trade_date"],
            "symbol": targets["symbol"].astype(str),
            "vol_20d": targets.get("daily_volatility_20", pd.Series([0.0] * len(targets))),
        }
    )
    regime = pd.DataFrame(
        {
            "date": [pd.Timestamp("2000-01-03")],
            "regime": ["tradable"],
            "total_weight_cap": [total_weight_cap],
        }
    )
    sized = size_positions(targets, volatilities, regime, cfg["risk"])
    return dict(zip(sized["symbol"].astype(str), sized["target_weight"].astype(float)))
