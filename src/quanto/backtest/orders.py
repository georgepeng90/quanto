import math

import pandas as pd

from quanto.strategies.base import Hold
from .models import Fill, Order, money


def create_orders(intent, portfolio, reference_prices, signal_date, execution_date, instruments) -> list[Order]:
    if isinstance(intent, Hold):
        return []
    metadata = instruments.set_index("symbol").to_dict("index")
    result = []
    for symbol in sorted(set(portfolio.shares) | set(intent.weights)):
        current = portfolio.shares.get(symbol, 0)
        weight = intent.weights.get(symbol, 0)
        if not current and not weight:
            continue
        price = reference_prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError(f"MISSING_REFERENCE_PRICE: {symbol}")
        lot = int(metadata[symbol]["lot_size"])
        desired = math.floor(portfolio.equity * weight / price / lot + 1e-12) * lot
        delta = desired - current
        quantity = abs(delta) if desired == 0 else abs(delta) // lot * lot
        if quantity:
            result.append(Order(f"{pd.Timestamp(signal_date).date()}:{symbol}:{'buy' if delta > 0 else 'sell'}",
                symbol, "buy" if delta > 0 else "sell", quantity, pd.Timestamp(signal_date),
                pd.Timestamp(execution_date) if execution_date is not None else None,
                reason=intent.reasons.get(symbol, "rebalance"), lot_size=lot,
                settlement_sessions=int(metadata[symbol]["settlement_sessions"]), full_exit=desired == 0))
    return result


def execute_open(orders, open_quotes, status, ledger, execution_spec) -> list[Fill]:
    fills = []

    def commission(quantity, price):
        return money(max(execution_spec.minimum_commission, quantity * price * execution_spec.commission_rate)) if quantity else 0

    def cost(order, quantity, price):
        return money(quantity * price + commission(quantity, price))

    def complete(order, quantity, price):
        if quantity:
            fill = Fill(order.id, order.execution_date, order.symbol, order.side, quantity, price,
                commission(quantity, price), money(quantity * abs(price - open_quotes[order.symbol])),
                ledger.settlement_date(order.execution_date, order.settlement_sessions) if order.side == "buy" else None)
            ledger.apply_fill(fill)
            fills.append(fill)
        order.filled_quantity = quantity
        order.expired_quantity = order.quantity - quantity
        order.status = "filled" if quantity == order.quantity else "expired"
        if order.expired_quantity and order.reason not in {"suspended", "cannot_buy", "cannot_sell", "insufficient_fee_cash"}:
            order.reason = "insufficient_cash" if order.side == "buy" else "insufficient_sellable"

    buys = []
    for order in sorted(orders, key=lambda o: (o.side != "sell", o.symbol, o.id)):
        if order.status != "pending":
            continue
        state = status.get(order.symbol, {})
        raw = open_quotes.get(order.symbol)
        if state.get("status") == "suspended" or not state.get(f"can_{order.side}", True):
            order.reason = "suspended" if state.get("status") == "suspended" else f"cannot_{order.side}"
            complete(order, 0, 0)
            continue
        if raw is None or not math.isfinite(raw) or raw <= 0:
            raise ValueError(f"MISSING_OPEN_PRICE: {order.symbol}")
        price = raw * (1 + execution_spec.slippage_rate if order.side == "buy" else 1 - execution_spec.slippage_rate)
        if price <= 0:
            raise ValueError("INVALID_EXECUTION_PRICE: 滑点后价格非正")
        if order.side == "sell":
            available = ledger.snapshot(order.execution_date).sellable_shares.get(order.symbol, 0)
            quantity = min(order.quantity, available)
            if not (order.full_exit and quantity == ledger.snapshot(order.execution_date).shares.get(order.symbol, 0)):
                quantity = quantity // order.lot_size * order.lot_size
            if commission(quantity, price) > quantity * price + ledger.cash:
                quantity = 0
                order.reason = "insufficient_fee_cash"
            complete(order, quantity, price)
        else:
            buys.append((order, price))
    # Allocate cash proportionally to frozen requests, then whole lots by symbol.
    required = sum(cost(o, o.quantity, p) for o, p in buys)
    ratio = min(1, ledger.cash / required) if required else 0
    allocations = []
    for order, price in buys:
        budget = cost(order, order.quantity, price) * ratio
        quantity = min(order.quantity, math.floor(budget / price / order.lot_size) * order.lot_size)
        while quantity and cost(order, quantity, price) > budget + 1e-8:
            quantity -= order.lot_size
        allocations.append(quantity)
    remaining = money(ledger.cash - sum(cost(o, q, p) for (o, p), q in zip(buys, allocations)))
    for index, (order, price) in enumerate(buys):
        quantity = allocations[index]
        # Binary search the deterministic residual award; avoids per-share loops.
        low, high = 0, (order.quantity - quantity) // order.lot_size
        while low < high:
            mid = (low + high + 1) // 2
            extra = cost(order, quantity + mid * order.lot_size, price) - cost(order, quantity, price)
            if extra <= remaining + 1e-8:
                low = mid
            else:
                high = mid - 1
        added = low * order.lot_size
        remaining = money(remaining - cost(order, quantity + added, price) + cost(order, quantity, price))
        allocations[index] += added
    for (order, price), quantity in zip(buys, allocations):
        complete(order, quantity, price)
    return fills
