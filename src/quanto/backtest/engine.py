from __future__ import annotations

from dataclasses import asdict
from datetime import date
import copy
import json
import random
from typing import Callable

import numpy as np
import pandas as pd

from quanto.config import BacktestSpec
from quanto.data.schemas import DatasetBundle
from quanto.risk.policy import RiskPolicy
from quanto.strategies.base import Hold, Strategy, StrategyContext, TargetPortfolio, is_week_end, validate_intent, validate_warmup_window
from .ledger import PortfolioLedger
from .models import BacktestCancelled, BacktestResult, RunEvent, TABLE_COLUMNS, money, whole_shares
from .orders import create_orders, execute_open


def _json_value(value):
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat() if not isinstance(value, pd.Timestamp) or value.tzinfo else value.date().isoformat()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"不可序列化的结果值: {type(value).__name__}")


def run_backtest(
    bundle: DatasetBundle,
    strategy: Strategy,
    spec: BacktestSpec,
    emit: Callable[[RunEvent], None],
    cancelled: Callable[[], bool],
) -> BacktestResult:
    """Complete each session atomically at emit; no filesystem output.

    Payload tables contain new daily rows, except orders which are changed rows
    to upsert by id. A failed/cancelled day is never emitted as complete.
    """
    spec.validate_for_dataset(bundle)
    calendar = pd.DatetimeIndex(bundle.calendar.date).sort_values().unique()
    dates = calendar[(calendar >= pd.Timestamp(spec.start)) & (calendar <= pd.Timestamp(spec.end))]
    prices = bundle.prices[bundle.prices.symbol.isin(spec.symbols)].sort_values(["date", "symbol"])
    observed = pd.DatetimeIndex(prices.date.unique()).sort_values()
    validate_warmup_window(strategy, observed, spec.start, spec.end)
    random.seed(spec.seed)
    np.random.seed(spec.seed % 2**32)
    ledger, risk = PortfolioLedger(spec.initial_cash), RiskPolicy(spec.risk, initial_equity=spec.initial_cash)
    ledger.calendar = calendar
    instruments = bundle.instruments[bundle.instruments.symbol.isin(spec.symbols)]
    groups = instruments.set_index("symbol")["group"].to_dict()
    approvals = bundle.auxiliary.get("risk_approvals", pd.DataFrame())
    if not approvals.empty:
        required = {"approval_id", "pause_asof", "available_at"}
        if not required.issubset(approvals.columns) or approvals.approval_id.isna().any() or approvals.approval_id.duplicated().any():
            raise ValueError("INVALID_APPROVAL: 批准记录需要唯一编号、暂停日期和可知时间")
    approval_rows = approvals.to_dict("records")
    for approval in approval_rows:
        available = pd.Timestamp(approval["available_at"])
        if available.tzinfo is None:
            raise ValueError("INVALID_APPROVAL_TIME: 批准时间必须包含时区")
    corporate = bundle.events[bundle.events.symbol.isin(spec.symbols)].reset_index(drop=True)
    # Adjusted research prices already incorporate corporate actions.
    if bundle.price_mode == "adjusted_research":
        corporate = corporate.iloc[:0]
    entitlements = {}
    orders, tables, previous_orders = [], {name: [] for name in TABLE_COLUMNS}, {}
    evaluated, completed_through = False, None
    price_groups = {d: frame for d, frame in prices.groupby("date")}
    status_groups = {d: frame.set_index("symbol").to_dict("index") for d, frame in bundle.status.groupby("date")}
    eligibility_groups = {d: set(frame.loc[frame.eligible, "symbol"]) for d, frame in bundle.eligibility.groupby("date")}

    for day_number, asof in enumerate(dates, 1):
        if cancelled():
            raise BacktestCancelled("CANCELLED: 回测已取消")
        offsets = {name: len(rows) for name, rows in tables.items()}

        def account(kind, symbol="", amount=0.0, **details):
            tables["account_events"].append(dict(date=asof, event_type=kind, symbol=symbol, amount=money(amount), details=details))

        for index, event in corporate.iterrows():
            if event.event_type == "split" and event.ex_date == asof:
                live = [order for order in orders if order.status == "pending" and order.symbol == event.symbol]
                quantities = [whole_shares(order.quantity * event.split_ratio) for order in live]
                ledger.apply_event(event)
                for order, quantity in zip(live, quantities):
                    order.quantity = quantity
                account("split", event.symbol, split_ratio=event.split_ratio)
            elif event.event_type == "dividend":
                amount = entitlements.get(index, 0.0)
                if event.ex_date == asof:
                    ledger.apply_event(dict(event_type="dividend_receivable", amount=amount))
                    account("dividend_receivable", event.symbol, amount)
                if event.pay_date == asof:
                    ledger.apply_event(dict(event_type="dividend_payment", amount=amount))
                    account("dividend_payment", event.symbol, amount)
        interest_base = ledger.cash
        interest = money(interest_base * spec.metrics.cash_rate / spec.metrics.annual_sessions)
        ledger.reserve_cash(max(0, -interest))
        daily = price_groups.get(asof, prices.iloc[:0])
        opens, closes = daily.set_index("symbol").open.to_dict(), daily.set_index("symbol").close.to_dict()
        status = status_groups.get(asof, {})
        scheduled = [order for order in orders if order.status == "pending" and order.execution_date == asof]
        fills = execute_open(scheduled, opens, status, ledger, spec.execution)
        tables["fills"].extend(asdict(fill) for fill in fills)
        ledger.apply_event(dict(event_type="interest", amount=interest))
        account("interest", amount=interest, opening_cash=interest_base, annual_rate=spec.metrics.cash_rate, annual_sessions=spec.metrics.annual_sessions)
        equity = ledger.mark(asof, closes, status, spec.execution.max_stale_sessions)
        portfolio = ledger.snapshot(asof)
        for index, event in corporate.iterrows():
            if event.event_type == "dividend" and event.record_date == asof:
                shares = portfolio.shares.get(event.symbol, 0)
                entitlements[index] = money(shares * event.cash_per_share)
                account("dividend_record", event.symbol, entitlements[index], entitled_shares=shares)
        eligible = tuple(sorted(eligibility_groups.get(asof, set()) & set(spec.symbols)))
        cutoff = (asof.normalize() + pd.Timedelta(hours=15)).tz_localize("Asia/Shanghai").tz_convert("UTC")
        known_approvals = [row for row in approval_rows if pd.Timestamp(row["available_at"]) <= cutoff]
        ctx = StrategyContext(asof, prices, eligible, portfolio,
            tuple(asdict(order) for order in orders if order.status == "pending"),
            {**risk.state, "symbol_groups": groups, "risk_approvals": known_approvals}, is_week_end(calendar, asof))
        proposed = Hold("预热或非评估日")
        # Warmup bars are prior observed dates, never empty calendar sessions.
        if observed.searchsorted(asof, side="left") >= strategy.warmup_bars and strategy.should_evaluate(ctx):
            evaluated = True
            proposed = strategy.on_close(ctx)
            validate_intent(proposed, set(spec.symbols))
        decision = risk.evaluate(ctx, proposed)
        tables["risk_events"].extend(decision.events)
        intent = decision.intent
        tables["signals"].append(copy.deepcopy(dict(date=asof, intent="hold" if isinstance(intent, Hold) else "target",
            weights={} if isinstance(intent, Hold) else intent.weights,
            reasons={"hold": intent.reason} if isinstance(intent, Hold) else intent.reasons,
            diagnostics={} if isinstance(intent, Hold) else intent.diagnostics)))
        if isinstance(intent, TargetPortfolio):
            for order in orders:
                if order.status == "pending":
                    order.status, order.reason = "cancelled", "superseded_target"
            next_index = calendar.get_loc(asof) + 1
            execution_date = calendar[next_index] if next_index < len(calendar) else None
            references = {**ledger.marks, **closes}
            new_orders = create_orders(intent, portfolio, references, asof, execution_date, instruments)
            for order in new_orders:
                if intent is not proposed:
                    order.source = "risk"
                if order.side == "buy" and order.symbol not in eligible:
                    order.status, order.reason = "cancelled", "ineligible"
                elif execution_date is None or execution_date > pd.Timestamp(spec.end):
                    order.status, order.reason = "unexecuted", "end_of_run"
            orders.extend(new_orders)
        tables["equity"].append(equity)
        for symbol in sorted(set(spec.symbols) | set(portfolio.shares)):
            value = portfolio.market_values.get(symbol, 0)
            basis = ledger.cost_basis(symbol)
            tables["positions"].append(dict(date=asof, symbol=symbol, shares=portfolio.shares.get(symbol, 0),
                sellable_shares=portfolio.sellable_shares.get(symbol, 0), market_value=value,
                weight=value / portfolio.equity if portfolio.equity else 0, cost_basis=basis,
                unrealized_pnl=money(value - basis), realized_pnl=ledger.realized.get(symbol, 0)))
        if cancelled():
            raise BacktestCancelled("CANCELLED: 回测已取消")
        completed_through = asof
        updates = []
        for order in orders:
            row = asdict(order)
            if previous_orders.get(order.id) != row:
                updates.append(row)
                previous_orders[order.id] = row
        delta = {name: rows[offsets[name]:] for name, rows in tables.items() if name != "orders"}
        delta["orders"] = updates
        payload = json.loads(json.dumps(dict(tables=delta, completed_through=asof.date().isoformat()), default=_json_value, allow_nan=False))
        emit(RunEvent(day_number, "backtest", asof.date().isoformat(), day_number, len(dates), "info", "DAY_COMPLETED", "交易日计算完成", payload))
    if not evaluated:
        raise ValueError("NO_EVALUATION: 预热后没有可评估的策略交易日")
    tables["orders"] = [asdict(order) for order in orders]
    return BacktestResult(**{name: pd.DataFrame(rows, columns=TABLE_COLUMNS[name]) for name, rows in tables.items()}, completed_through=completed_through)
