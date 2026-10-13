from __future__ import annotations

from collections import deque
import math

import numpy as np
import pandas as pd

from quanto.config import BacktestSpec

from .models import BacktestResult, money


MATCH_COLUMNS = ["symbol", "entry_date", "exit_date", "quantity", "cost_basis", "proceeds", "realized_pnl", "holding_sessions"]
OPEN_COLUMNS = ["symbol", "entry_date", "quantity", "cost_basis", "holding_sessions"]


def max_drawdown(equity: pd.Series, initial_cash: float | None = None) -> float:
    values = equity.astype(float)
    if initial_cash is not None:
        values = pd.concat([pd.Series([initial_cash]), values], ignore_index=True)
    if values.empty:
        return 0.0
    return abs(float((values / values.cummax() - 1).min()))


def match_trades(result: BacktestResult) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Replay filled shares and splits in ledger order; match sell fees to FIFO lots."""
    lots: dict[str, deque[dict]] = {}
    matched: list[dict] = []
    sessions = {pd.Timestamp(day): index for index, day in enumerate(result.equity.date)}
    actions: list[tuple[pd.Timestamp, int, int, str, object]] = []
    for index, row in result.account_events.iterrows():
        if row.event_type == "split":
            actions.append((pd.Timestamp(row.date), 0, index, "split", row))
    for index, row in result.fills.iterrows():
        actions.append((pd.Timestamp(row.trade_date), 1, index, "fill", row))
    completed_episodes = 0
    for day, _, _, kind, row in sorted(actions, key=lambda item: item[:3]):
        symbol = str(row.symbol)
        queue = lots.setdefault(symbol, deque())
        if kind == "split":
            ratio = float(row.details["split_ratio"])
            for lot in queue:
                lot["quantity"] = int(round(lot["quantity"] * ratio))
            continue
        quantity = int(row.quantity)
        price = float(row.price)
        fees = float(row.commission + row.stamp_duty + row.transfer_fee)
        if row.side == "buy":
            queue.append({"entry_date": day, "quantity": quantity, "basis": money(quantity * price + fees)})
            continue
        remaining = quantity
        while remaining:
            if not queue:
                raise ValueError("UNMATCHED_SELL: 卖出数量超过 FIFO 库存")
            lot = queue[0]
            take = min(remaining, lot["quantity"])
            basis = lot["basis"] if take == lot["quantity"] else money(lot["basis"] * take / lot["quantity"])
            proceeds = money(take * price - fees * take / quantity)
            matched.append(dict(symbol=symbol, entry_date=lot["entry_date"], exit_date=day,
                quantity=take, cost_basis=basis, proceeds=proceeds, realized_pnl=money(proceeds - basis),
                holding_sessions=sessions[day] - sessions[lot["entry_date"]]))
            lot["quantity"] -= take
            lot["basis"] = money(lot["basis"] - basis)
            remaining -= take
            if lot["quantity"] == 0:
                queue.popleft()
        if not queue:
            completed_episodes += 1
    last_session = len(sessions) - 1
    open_rows = [dict(symbol=symbol, entry_date=lot["entry_date"], quantity=lot["quantity"],
        cost_basis=lot["basis"], holding_sessions=last_session - sessions[lot["entry_date"]])
        for symbol, queue in sorted(lots.items()) for lot in queue]
    return pd.DataFrame(matched, columns=MATCH_COLUMNS), pd.DataFrame(open_rows, columns=OPEN_COLUMNS), completed_episodes


def compute_metrics(result: BacktestResult, spec: BacktestSpec) -> dict[str, float | int | None]:
    if result.equity.empty:
        raise ValueError("EMPTY_EQUITY: 没有已完成的绩效日")
    equity = result.equity["equity"].astype(float).reset_index(drop=True)
    if not np.isfinite(equity.to_numpy()).all() or (equity <= 0).any():
        raise ValueError("INVALID_EQUITY: 权益必须为有限正数")
    initial = spec.initial_cash
    returns = equity.div(pd.concat([pd.Series([initial]), equity.iloc[:-1]], ignore_index=True)).sub(1)
    observed = len(returns)
    annual_sessions = spec.metrics.annual_sessions
    total_return = float(equity.iloc[-1] / initial - 1)
    annual_return = float((equity.iloc[-1] / initial) ** (annual_sessions / observed) - 1) if observed >= 2 else None
    volatility = float(returns.std(ddof=0) * math.sqrt(annual_sessions)) if observed >= 2 else None
    excess = returns - spec.metrics.risk_free_rate / annual_sessions
    sharpe = float(excess.mean() * math.sqrt(annual_sessions) / returns.std(ddof=0)) if volatility is not None and volatility > 0 else None
    drawdown = max_drawdown(equity, initial)
    calmar = float(annual_return / drawdown) if annual_return is not None and drawdown > 0 else None
    matched, open_lots, rounds = match_trades(result)
    weighted_days = (matched.holding_sessions * matched.quantity).sum() / matched.quantity.sum() if not matched.empty else None
    events = result.account_events
    income = lambda kind: float(events.loc[events.event_type == kind, "amount"].astype(float).sum()) if not events.empty else 0.0
    fills = result.fills
    traded_notional = float((fills.quantity.astype(float) * fills.price.astype(float)).sum()) if not fills.empty else 0.0
    commission_paid = float(fills.commission.astype(float).sum()) if not fills.empty else 0.0
    stamp_duty_paid = float(fills.stamp_duty.astype(float).sum()) if not fills.empty else 0.0
    transfer_fee_paid = float(fills.transfer_fee.astype(float).sum()) if not fills.empty else 0.0
    slippage_cost = float(fills.slippage_cost.astype(float).sum()) if not fills.empty else 0.0
    final_positions = result.positions.loc[pd.to_datetime(result.positions.date) == pd.Timestamp(result.equity.date.iloc[-1])]
    metrics = dict(total_return=total_return, annual_return=annual_return, volatility=volatility,
        sharpe=sharpe, max_drawdown=drawdown, calmar=calmar, final_equity=float(equity.iloc[-1]),
        fill_count=int(len(result.fills)), round_trip_count=rounds, open_lot_count=int(len(open_lots)),
        realized_pnl=float(matched.realized_pnl.sum()),
        unrealized_pnl=float(final_positions.unrealized_pnl.astype(float).sum()),
        turnover=traded_notional / float(equity.mean()),
        commission_paid=commission_paid, slippage_cost=slippage_cost,
        stamp_duty_paid=stamp_duty_paid, transfer_fee_paid=transfer_fee_paid,
        execution_cost=commission_paid + stamp_duty_paid + transfer_fee_paid + slippage_cost,
        average_invested_ratio=float((result.equity.market_value.astype(float).reset_index(drop=True) / equity).mean()),
        average_holding_sessions=float(weighted_days) if weighted_days is not None else None,
        dividend_income=income("dividend_payment"), interest_income=income("interest"))
    if any(isinstance(value, float) and not math.isfinite(value) for value in metrics.values()):
        raise ValueError("INVALID_METRIC: 指标含非有限数值")
    return metrics
