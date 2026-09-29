from dataclasses import dataclass

import pandas as pd

from quanto.strategies.base import PortfolioView
from .models import Fill, money, whole_shares


@dataclass
class Lot:
    quantity: int
    basis: float
    sellable_date: pd.Timestamp


class PortfolioLedger:
    """FIFO inventory; fees belong to basis/proceeds, dividends are income."""

    def __init__(self, initial_cash: float):
        self.cash = money(initial_cash)
        self.receivables = 0.0
        self.lots: dict[str, list[Lot]] = {}
        self.marks: dict[str, float] = {}
        self.stale_sessions: dict[str, int] = {}
        self.realized: dict[str, float] = {}
        self.calendar = pd.DatetimeIndex([])

    @property
    def realized_pnl(self) -> float:
        return money(sum(self.realized.values()))

    def cost_basis(self, symbol: str) -> float:
        return money(sum(lot.basis for lot in self.lots.get(symbol, [])))

    def settlement_date(self, asof, sessions):
        asof = pd.Timestamp(asof)
        if len(self.calendar):
            index = self.calendar.get_loc(asof) + sessions
            return self.calendar[index] if index < len(self.calendar) else pd.Timestamp.max.normalize()
        return asof + pd.offsets.BDay(sessions)

    def snapshot(self, asof) -> PortfolioView:
        shares = {s: sum(lot.quantity for lot in lots) for s, lots in self.lots.items()}
        sellable = {s: sum(lot.quantity for lot in lots if lot.sellable_date <= pd.Timestamp(asof)) for s, lots in self.lots.items()}
        values = {s: money(q * self.marks.get(s, 0)) for s, q in shares.items()}
        stale = sum(v for s, v in values.items() if self.stale_sessions.get(s, 0))
        return PortfolioView(self.cash, self.receivables, shares, sellable, values,
                             money(self.cash + self.receivables + sum(values.values())), money(stale))

    def apply_fill(self, fill: Fill) -> None:
        notional = money(fill.quantity * fill.price)
        lots = self.lots.setdefault(fill.symbol, [])
        if fill.side == "buy":
            cost = money(notional + fill.commission)
            if cost > self.cash + 1e-8:
                raise ValueError("INSUFFICIENT_CASH: 可用现金不足")
            self.cash = money(self.cash - cost)
            lots.append(Lot(fill.quantity, cost, fill.sellable_date if fill.sellable_date is not None else self.settlement_date(fill.trade_date, 1)))
        else:
            if fill.quantity > self.snapshot(fill.trade_date).sellable_shares.get(fill.symbol, 0):
                raise ValueError("INSUFFICIENT_SELLABLE: 可卖份额不足")
            remaining, basis = fill.quantity, 0.0
            for lot in lots:
                if lot.sellable_date > fill.trade_date:
                    continue
                quantity = min(remaining, lot.quantity)
                allocated = money(lot.basis * quantity / lot.quantity) if lot.quantity else 0
                lot.quantity -= quantity
                lot.basis = money(lot.basis - allocated)
                basis += allocated
                remaining -= quantity
                if remaining == 0:
                    break
            self.lots[fill.symbol] = [lot for lot in lots if lot.quantity]
            proceeds = money(notional - fill.commission)
            self.cash = money(self.cash + proceeds)
            self.realized[fill.symbol] = money(self.realized.get(fill.symbol, 0) + proceeds - basis)
        self.marks[fill.symbol] = fill.price

    def apply_event(self, event) -> None:
        kind = event["event_type"]
        amount = money(event.get("amount", 0))
        if kind == "dividend_receivable":
            self.receivables = money(self.receivables + amount)
        elif kind == "dividend_payment":
            self.receivables = money(self.receivables - amount)
            self.cash = money(self.cash + amount)
        elif kind == "interest":
            self.cash = money(self.cash + amount)
        elif kind == "split":
            symbol, ratio = event["symbol"], event["split_ratio"]
            lots = self.lots.get(symbol, [])
            quantities = [whole_shares(lot.quantity * ratio) for lot in lots]
            for lot, quantity in zip(lots, quantities):
                lot.quantity = quantity
            if symbol in self.marks:
                self.marks[symbol] /= ratio
        else:
            raise ValueError(f"UNKNOWN_ACCOUNT_EVENT: {kind}")

    def mark(self, asof, prices, status, max_stale_sessions) -> dict:
        for symbol, lots in self.lots.items():
            if not sum(lot.quantity for lot in lots):
                continue
            quote = prices.get(symbol)
            if status.get(symbol, {}).get("status") == "suspended":
                count = self.stale_sessions.get(symbol, 0) + 1
                if symbol not in self.marks or count > max_stale_sessions:
                    raise ValueError(f"STALE_PRICE_LIMIT: {symbol} 停牌估值超出上限")
                self.stale_sessions[symbol] = count
            elif quote is None or pd.isna(quote) or quote <= 0:
                raise ValueError(f"MISSING_PRICE: {symbol} 缺少可信价格")
            else:
                self.marks[symbol] = float(quote)
                self.stale_sessions[symbol] = 0
        view = self.snapshot(asof)
        return dict(date=pd.Timestamp(asof), cash=view.cash, receivables=view.receivables,
                    market_value=money(sum(view.market_values.values())), equity=view.equity, stale_value=view.stale_value)
