from __future__ import annotations

import math

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return abs(float(dd.min()))


def compute_metrics(equity_curve: pd.DataFrame, trades: pd.DataFrame | None = None) -> dict[str, float | int]:
    equity = equity_curve["equity"].astype(float)
    returns = equity.pct_change().fillna(0.0)
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0) if len(equity) > 1 else 0.0
    days = max((pd.to_datetime(equity_curve["date"].iloc[-1]) - pd.to_datetime(equity_curve["date"].iloc[0])).days, 1)
    annual_return = float((1.0 + total_return) ** (365.0 / days) - 1.0)
    vol = float(returns.std(ddof=0) * math.sqrt(252))
    sharpe = float((returns.mean() * 252) / vol) if vol > 0 else 0.0
    mdd = max_drawdown(equity)
    calmar = float(annual_return / mdd) if mdd > 0 else 0.0
    trade_count = 0 if trades is None else int(len(trades))
    return {
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": mdd,
        "sharpe": sharpe,
        "calmar": calmar,
        "trade_count": trade_count,
        "final_equity": float(equity.iloc[-1]) if not equity.empty else 0.0,
    }


def calculate_metrics(equity_curve: pd.DataFrame, annual_trading_days: int = 252) -> dict[str, float | int]:
    return compute_metrics(equity_curve, None)
