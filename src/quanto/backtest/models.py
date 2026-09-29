from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class Order:
    id: str
    symbol: str
    side: str
    quantity: int
    signal_date: pd.Timestamp
    execution_date: pd.Timestamp | None
    status: str = "pending"
    reason: str = "rebalance"
    source: str = "strategy"
    filled_quantity: int = 0
    expired_quantity: int = 0
    lot_size: int = 1
    settlement_sessions: int = 1
    full_exit: bool = False


@dataclass(frozen=True)
class Fill:
    order_id: str
    trade_date: pd.Timestamp
    symbol: str
    side: str
    quantity: int
    price: float
    commission: float
    slippage_cost: float
    sellable_date: pd.Timestamp | None = None


TABLE_COLUMNS = {
    "equity": ["date", "cash", "receivables", "market_value", "equity", "stale_value"],
    "orders": list(Order.__dataclass_fields__),
    "fills": list(Fill.__dataclass_fields__),
    "positions": ["date", "symbol", "shares", "sellable_shares", "market_value", "weight", "cost_basis", "unrealized_pnl", "realized_pnl"],
    "signals": ["date", "intent", "weights", "reasons", "diagnostics"],
    "risk_events": ["date", "code", "message", "details"],
    "account_events": ["date", "event_type", "symbol", "amount", "details"],
}


@dataclass(frozen=True)
class BacktestResult:
    equity: pd.DataFrame
    orders: pd.DataFrame
    fills: pd.DataFrame
    positions: pd.DataFrame
    signals: pd.DataFrame
    risk_events: pd.DataFrame
    account_events: pd.DataFrame
    completed_through: pd.Timestamp | None


@dataclass(frozen=True)
class RunEvent:
    seq: int
    phase: str
    asof: str
    completed_days: int
    total_days: int
    level: str
    code: str
    message: str
    payload: dict[str, Any]


class BacktestCancelled(Exception):
    """The caller retains completed days through the emit boundary."""


def money(value: float) -> float:
    """Account amounts use 12 decimal places, rounded ties to even."""
    return round(float(value), 12)


def whole_shares(value: float) -> int:
    rounded = round(value)
    if abs(value - rounded) > 1e-8:
        raise ValueError("FRACTIONAL_SHARES: 不支持零碎股份或现金替代")
    return int(rounded)
