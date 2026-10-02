"""Streaming, next-open research ledger for completed intraday bars.

The engine keeps account state and one timestamp group, never output history.
Prices are research marks; corporate-action cash flows are not reconstructed.
"""
from __future__ import annotations

from contextlib import ExitStack
import csv
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from itertools import groupby
import json
import math
from pathlib import Path


def _utc(value, name):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an aware UTC timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must be an aware UTC timestamp")
    return result.astimezone(timezone.utc)


def _number(value, name, low, high=None, integer=False):
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not math.isfinite(value) or value < low or (high is not None and value > high):
        raise ValueError(f"Invalid {name}: expected {'integer' if integer else 'number'} in [{low}, {high}]")
    return value


@dataclass(frozen=True)
class IntradaySpec:
    start: str
    end: str
    initial_cash: float = 100000
    top_k: int = 5
    rebalance_every: int = 1
    max_total_weight: float = 1
    max_symbol_weight: float = 1
    commission_rate: float = .0003
    minimum_commission: float = 5
    slippage_rate: float = .0005
    participation_rate: float = .1
    settlement_days: int = 1
    lot_size: int = 100
    drawdown_pause: float = 1
    cash_rate: float = 0
    baseline: str = "model"
    instruments: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Backtest spec must be an object")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown backtest settings: {sorted(unknown)}")
        if "start" not in value or "end" not in value:
            raise ValueError("Backtest start and end are required")
        start, end = _utc(value["start"], "start"), _utc(value["end"], "end")
        if end <= start:
            raise ValueError("Backtest end must be after start")
        spec = cls(**(value | {"start": start.isoformat(), "end": end.isoformat()}))
        for name in ("top_k", "rebalance_every", "lot_size"):
            _number(getattr(spec, name), name, 1, integer=True)
        _number(spec.settlement_days, "settlement_days", 0, integer=True)
        _number(spec.initial_cash, "initial_cash", 0)
        if spec.initial_cash == 0:
            raise ValueError("initial_cash must be positive")
        for name in ("max_total_weight", "max_symbol_weight", "participation_rate", "drawdown_pause"):
            _number(getattr(spec, name), name, 0, 1)
        for name in ("commission_rate", "slippage_rate"):
            _number(getattr(spec, name), name, 0, .999999)
        _number(spec.minimum_commission, "minimum_commission", 0)
        _number(spec.cash_rate, "cash_rate", 0, 1)
        if spec.baseline not in ("model", "equal_weight", "momentum", "cash"):
            raise ValueError("baseline must be model, equal_weight, momentum or cash")
        if not isinstance(spec.instruments, dict):
            raise ValueError("instruments must be an object")
        for symbol, config in spec.instruments.items():
            if not isinstance(symbol, str) or not symbol or not isinstance(config, dict):
                raise ValueError("Instrument settings require instrument IDs and objects")
            if set(config) - {"lot_size", "settlement_days"}:
                raise ValueError("Unknown instrument settings")
            for name, value in config.items():
                _number(value, name, 1 if name == "lot_size" else 0, integer=True)
        return spec

    def instrument(self, symbol, key):
        return self.instruments.get(symbol, {}).get(key, getattr(self, key))


_COLUMNS = {
    "equity": ["timestamp", "trading_date", "cash", "market_value", "equity", "drawdown", "paused"],
    "daily_equity": ["timestamp", "trading_date", "cash", "market_value", "equity", "return", "interest"],
    "orders": ["id", "instrument_id", "side", "signal_time", "timestamp", "trading_date", "quantity",
               "filled_quantity", "remaining_quantity", "status", "reason", "lot_size", "settlement_days"],
    "fills": ["id", "order_id", "instrument_id", "side", "signal_time", "timestamp", "bar_start", "bar_end",
              "trading_date", "quantity", "price", "commission", "slippage_cost", "cash_after"],
    "signals": ["timestamp", "bar_start", "bar_end", "trading_date", "instrument_id", "score", "target_weight", "reason"],
    "positions": ["timestamp", "trading_date", "instrument_id", "quantity", "sellable_quantity", "price",
                  "market_value", "weight", "cost_basis", "unrealized_pnl", "realized_pnl"],
}


def _bars(rows, start, end, cancelled):
    previous = None
    ends = {}
    for original in rows:
        _check_cancelled(cancelled)
        row = dict(original)
        symbol = row.get("instrument_id")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("A bar requires instrument_id")
        row["bar_start"] = _utc(row.get("bar_start"), "bar_start")
        row["bar_end"] = _utc(row.get("bar_end"), "bar_end")
        key = (row["bar_end"], symbol)
        if previous is not None and key <= previous:
            raise ValueError("Rows must be strictly sorted by (bar_end, instrument_id), with no duplicates")
        previous = key
        if row["bar_end"] <= row["bar_start"]:
            raise ValueError("A bar must have positive duration")
        if symbol in ends and row["bar_start"] < ends[symbol]:
            raise ValueError("Overlapping bars are not supported")
        ends[symbol] = row["bar_end"]
        try:
            row["trading_date"] = date.fromisoformat(str(row["trading_date"])).isoformat()
        except (KeyError, ValueError) as exc:
            raise ValueError("A bar requires ISO trading_date") from exc
        for name in ("open", "high", "low", "close", "volume", "amount"):
            _number(row.get(name), name, 0)
            row[name] = float(row[name])
        if row["low"] <= 0 or row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
            raise ValueError("Invalid positive OHLC prices")
        score = row.get("score")
        if score is not None:
            _number(score, "score", -math.inf)
        if row["bar_start"] >= end:
            break
        if row["bar_start"] >= start and row["bar_end"] <= end:
            yield row


def _check_cancelled(cancelled):
    if cancelled():
        from quanto.backtest.models import BacktestCancelled

        raise BacktestCancelled("Intraday backtest cancelled")


@dataclass
class _Position:
    quantity: int = 0
    cost_basis: float = 0
    realized_pnl: float = 0
    # One bucket per unsettled acquisition day, merged and removed on settlement.
    locked: dict = field(default_factory=dict)

    def sellable(self, day_index):
        for unlock in list(self.locked):
            if unlock <= day_index:
                del self.locked[unlock]
        return self.quantity - sum(self.locked.values())


class _Ledger:
    def __init__(self, spec, writers, metadata, emit, cancelled):
        self.spec, self.writers = spec, writers
        self.emit, self.cancelled = emit, cancelled
        self.cash = float(spec.initial_cash)
        self.positions, self.prices, self.previous_closes = {}, {}, {}
        self.pending = {}
        self.day, self.day_index = None, -1
        self.peak = self.cash
        self.max_drawdown = 0
        self.paused = False
        self.counts = dict(bars=0, rows=0, orders=0, fills=0, trading_days=0, signals=0)
        self.last_equity = None
        self.first_time, self.last_time = None, None
        self.daily_previous = self.cash
        self.daily_mean, self.daily_m2, self.daily_peak, self.daily_drawdown = 0, 0, self.cash, 0
        self.fees, self.slippage, self.turnover, self.interest = 0, 0, 0, 0
        calendar = metadata.get("trading_days")
        self.calendar = None
        if calendar is not None:
            if not isinstance(calendar, list) or not calendar:
                raise ValueError("metadata trading_days must be a nonempty ordered list")
            try:
                days = [date.fromisoformat(str(day)).isoformat() for day in calendar]
            except ValueError as exc:
                raise ValueError("Invalid calendar trading day") from exc
            if days != sorted(set(days)):
                raise ValueError("Calendar trading_days must be sorted and unique")
            self.calendar = {day: index for index, day in enumerate(days)}

    def write(self, name, value):
        self.writers[name].writerow(value)

    def market_value(self):
        return sum(position.quantity * self.prices[symbol] for symbol, position in self.positions.items())

    def observe_equity(self):
        equity = self.cash + self.market_value()
        self.peak = max(self.peak, equity)
        drawdown = 1 - equity / self.peak
        self.max_drawdown = max(self.max_drawdown, drawdown)
        if drawdown >= self.spec.drawdown_pause and drawdown > 0:
            self.paused = True
        return equity, drawdown

    def finish_bar(self, final_day=False):
        if self.last_equity is None:
            return
        if final_day:
            interest = self.cash * self.spec.cash_rate / 252
            self.cash += interest
            self.interest += interest
            equity, drawdown = self.observe_equity()
            self.last_equity.update(cash=self.cash, equity=equity, drawdown=drawdown, paused=self.paused)
            daily_return = equity / self.daily_previous - 1
            self.counts["trading_days"] += 1
            n = self.counts["trading_days"]
            delta = daily_return - self.daily_mean
            self.daily_mean += delta / n
            self.daily_m2 += delta * (daily_return - self.daily_mean)
            self.daily_previous = equity
            self.daily_peak = max(self.daily_peak, equity)
            self.daily_drawdown = max(self.daily_drawdown, 1 - equity / self.daily_peak)
            self.write("daily_equity", {key: self.last_equity[key] for key in ("timestamp", "trading_date", "cash", "market_value", "equity")} |
                       {"return": daily_return, "interest": interest})
            self.emit({"phase": "intraday", "asof": self.day, "completed_days": n, "total_days": 0,
                       "level": "info", "code": "INTRADAY_DAY_COMPLETED", "message": "Intraday trading day completed",
                       "payload": {"completed_through": self.last_time, "equity": equity, "bars": self.counts["bars"]}})
        self.write("equity", self.last_equity)
        equity = self.last_equity["equity"]
        for symbol, position in sorted(self.positions.items()):
            if not position.quantity:
                continue
            value = position.quantity * self.prices[symbol]
            self.write("positions", dict(timestamp=self.last_time, trading_date=self.day, instrument_id=symbol,
                                         quantity=position.quantity, sellable_quantity=position.sellable(self.day_index),
                                         price=self.prices[symbol], market_value=value, weight=value / equity if equity else 0,
                                         cost_basis=position.cost_basis, unrealized_pnl=value - position.cost_basis,
                                         realized_pnl=position.realized_pnl))
        self.last_equity = None

    def finish_order(self, order, quantity=0, reason="filled", row=None):
        self.write("orders", {key: value for key, value in order.items() if key in _COLUMNS["orders"]} |
                   dict(timestamp=row["bar_start"].isoformat() if row else "", trading_date=row["trading_date"] if row else "",
                        filled_quantity=quantity, remaining_quantity=order["quantity"] - quantity,
                        status="filled" if quantity == order["quantity"] else "partial" if quantity else "expired", reason=reason))

    def execute(self, order, row):
        spec, symbol = self.spec, order["instrument_id"]
        position = self.positions.setdefault(symbol, _Position())
        lot = order["lot_size"]
        desired = order["quantity"]
        quantity = desired
        reason = "filled"
        cap = int(order["volume"] * spec.participation_rate // lot) * lot
        if quantity > cap:
            quantity, reason = cap, "participation"
        side = order["side"]
        open_price = row["open"]
        price = open_price * (1 + spec.slippage_rate if side == "buy" else 1 - spec.slippage_rate)
        if side == "sell":
            available = position.sellable(self.day_index) // lot * lot
            if quantity > available:
                quantity, reason = available, "settlement"
            # Never allow an uneconomic minimum-fee sale to overdraw the account.
            if quantity and self.cash + quantity * price < max(spec.minimum_commission, quantity * price * spec.commission_rate):
                quantity, reason = 0, "insufficient_cash"
        else:
            if self.paused:
                self.finish_order(order, reason="drawdown_pause", row=row)
                return
            if not position.quantity and sum(p.quantity > 0 for p in self.positions.values()) >= spec.top_k:
                self.finish_order(order, reason="top_k", row=row)
                return
            affordable = min(self.cash / (price * (1 + spec.commission_rate)),
                             max(0, self.cash - spec.minimum_commission) / price)
            affordable = int((affordable + 1e-10) // lot) * lot
            if quantity > affordable:
                quantity, reason = affordable, "insufficient_cash"
            market = self.market_value()
            equity = self.cash + market
            for weight, current in ((spec.max_total_weight, market),
                                    (spec.max_symbol_weight, position.quantity * open_price)):
                budget = weight * equity - current
                # Caps use post-fill equity, including both possible commission regimes.
                slip_per_share = price - open_price
                max_quantity = min((budget - weight * spec.minimum_commission) / (open_price + weight * slip_per_share),
                                   budget / (open_price + weight * (slip_per_share + price * spec.commission_rate)))
                max_quantity = max(0, int((max_quantity + 1e-10) // lot) * lot)
                if quantity > max_quantity:
                    quantity, reason = max_quantity, "weight_cap"
            # A candidate's costs lower equity for every existing holding, too.
            largest_other = max((p.quantity * self.prices[s] for s, p in self.positions.items() if s != symbol), default=0)
            if quantity and largest_other and spec.max_symbol_weight > 0:
                cost_budget = equity - largest_other / spec.max_symbol_weight
                if cost_budget + 1e-10 < spec.minimum_commission:
                    quantity, reason = 0, "weight_cap"
                else:
                    slip_per_share = price - open_price
                    variable_cost = slip_per_share + price * spec.commission_rate
                    max_quantity = quantity
                    if variable_cost > 0:
                        max_quantity = min(max_quantity, int((cost_budget / variable_cost + 1e-10) // lot) * lot)
                    if slip_per_share > 0:
                        max_quantity = min(max_quantity, int(((cost_budget - spec.minimum_commission) / slip_per_share + 1e-10) // lot) * lot)
                    if quantity > max_quantity:
                        quantity, reason = max(0, max_quantity), "weight_cap"
        if quantity:
            gross = quantity * price
            commission = max(spec.minimum_commission, gross * spec.commission_rate)
            if side == "buy":
                self.cash -= gross + commission
                position.quantity += quantity
                position.cost_basis += gross + commission
                unlock = self.day_index + order["settlement_days"]
                if unlock > self.day_index:
                    position.locked[unlock] = position.locked.get(unlock, 0) + quantity
            else:
                basis = position.cost_basis * quantity / position.quantity
                self.cash += gross - commission
                position.quantity -= quantity
                position.cost_basis -= basis
                position.realized_pnl += gross - commission - basis
            if abs(self.cash) < 1e-9:
                self.cash = 0
            self.fees += commission
            slippage = quantity * abs(price - open_price)
            self.slippage += slippage
            self.turnover += gross
            self.counts["fills"] += 1
            self.observe_equity()
            self.write("fills", dict(id=f"{order['id']}:fill", order_id=order["id"], instrument_id=symbol,
                                     side=side, signal_time=order["signal_time"], timestamp=row["bar_start"].isoformat(),
                                     bar_start=row["bar_start"].isoformat(), bar_end=row["bar_end"].isoformat(),
                                     trading_date=self.day, quantity=quantity, price=price, commission=commission,
                                     slippage_cost=slippage, cash_after=self.cash))
        self.finish_order(order, quantity, reason if quantity < desired else "filled", row)

    def signal(self, bars, equity):
        scores = {}
        for row in bars:
            symbol = row["instrument_id"]
            if self.spec.baseline == "equal_weight":
                score = 1
            elif self.spec.baseline == "momentum":
                old = self.previous_closes.get(symbol)
                score = row["close"] / old - 1 if old else None
            elif self.spec.baseline == "cash":
                score = None
            else:
                score = row.get("score")
            scores[symbol] = score
        selected = sorted((symbol for symbol, score in scores.items() if score is not None and score > 0),
                          key=lambda symbol: (-scores[symbol], symbol))[:self.spec.top_k]
        weight = min(self.spec.max_symbol_weight, self.spec.max_total_weight / len(selected)) if selected else 0
        targets = {symbol: weight for symbol in selected} if not self.paused else {}
        row_map = {row["instrument_id"]: row for row in bars}
        for row in bars:
            symbol = row["instrument_id"]
            self.write("signals", dict(timestamp=self.last_time, bar_start=row["bar_start"].isoformat(),
                                       bar_end=self.last_time, trading_date=self.day, instrument_id=symbol,
                                       score=scores[symbol], target_weight=targets.get(symbol, 0),
                                       reason="drawdown_pause" if self.paused else self.spec.baseline))
            self.counts["signals"] += 1
        for symbol in sorted(set(targets) | {s for s, position in self.positions.items() if position.quantity}):
            # A missing bar supplies no completed price/volume for a new order.
            if symbol not in row_map:
                continue
            row = row_map[symbol]
            lot = self.spec.instrument(symbol, "lot_size")
            target = int((equity * targets.get(symbol, 0) / row["close"] + 1e-10) // lot) * lot
            current = self.positions.get(symbol, _Position()).quantity
            delta = target - current
            if symbol in self.pending:
                self.finish_order(self.pending.pop(symbol), reason="superseded")
            if not delta:
                continue
            self.counts["orders"] += 1
            self.pending[symbol] = dict(id=f"{self.last_time}:{symbol}:{self.counts['orders']}", instrument_id=symbol,
                                        side="buy" if delta > 0 else "sell", signal_time=self.last_time,
                                        quantity=abs(delta), lot_size=lot,
                                        settlement_days=self.spec.instrument(symbol, "settlement_days"), volume=row["volume"])

    def process(self, bars):
        _check_cancelled(self.cancelled)
        dates = {row["trading_date"] for row in bars}
        starts = {row["bar_start"] for row in bars}
        if len(dates) != 1 or len(starts) != 1:
            raise ValueError("Each completed timestamp group must share trading_date and bar_start")
        day = next(iter(dates))
        if self.day is not None and day < self.day:
            raise ValueError("Trading dates must be chronological")
        if self.calendar is not None and day != self.day:
            if day not in self.calendar:
                raise ValueError(f"Trading day {day} absent from supplied calendar")
            if self.day is not None and self.calendar[day] != self.day_index + 1:
                missing = next(calendar_day for calendar_day, index in self.calendar.items() if index == self.day_index + 1)
                raise ValueError(f"Missing complete trading day {missing}; supply coverage for every calendar session between observed days")
        self.finish_bar(final_day=self.day is not None and day != self.day)
        if day != self.day:
            self.day = day
            if self.calendar is None:
                self.day_index += 1
            else:
                self.day_index = self.calendar[day]
        self.counts["bars"] += 1
        self.counts["rows"] += len(bars)
        self.last_time = bars[0]["bar_end"].isoformat()
        self.first_time = self.first_time or self.last_time
        # At this point only opening prices are used; current volume/close never sizes fills.
        for row in bars:
            self.prices[row["instrument_id"]] = row["open"]
        self.observe_equity()
        due = [(self.pending[row["instrument_id"]], row) for row in bars
               if row["instrument_id"] in self.pending and row["bar_start"] >= _utc(self.pending[row["instrument_id"]]["signal_time"], "signal_time")]
        for order, row in sorted(due, key=lambda pair: (pair[0]["side"] == "buy", pair[0]["instrument_id"])):
            self.pending.pop(order["instrument_id"])
            self.execute(order, row)
        for row in bars:
            self.prices[row["instrument_id"]] = row["close"]
        equity, drawdown = self.observe_equity()
        if self.paused or (self.counts["bars"] - 1) % self.spec.rebalance_every == 0:
            self.signal(bars, equity)
        for row in bars:
            self.previous_closes[row["instrument_id"]] = row["close"]
        self.last_equity = dict(timestamp=self.last_time, trading_date=day, cash=self.cash,
                                market_value=self.market_value(), equity=equity, drawdown=drawdown, paused=self.paused)

    def metrics(self):
        equity = self.cash + self.market_value()
        n = self.counts["trading_days"]
        variance = self.daily_m2 / (n - 1) if n > 1 else 0
        exponent = math.log(equity / self.spec.initial_cash) * 252 / n if n and equity > 0 else None
        annualized = math.expm1(exponent) if exponent is not None and exponent < 709 else None
        return dict(initial_cash=self.spec.initial_cash, final_cash=self.cash, final_equity=equity,
                    total_return=equity / self.spec.initial_cash - 1, annualized_return=annualized,
                    sharpe=self.daily_mean / math.sqrt(variance) * math.sqrt(252) if variance > 1e-20 else None,
                    max_drawdown=self.daily_drawdown, minute_max_drawdown=self.max_drawdown,
                    trading_days=n, commissions=self.fees, slippage_cost=self.slippage,
                    turnover=self.turnover / self.spec.initial_cash, cash_interest=self.interest)


def run_intraday_backtest(rows, spec: dict, output_dir: Path, emit, cancelled, metadata: dict) -> dict:
    """Write bounded CSV streams and return the JSON-compatible research summary.

    The inclusive bar-start/exclusive bar-end coverage is confined to ``[start,end)``:
    a bar ending exactly at ``end`` is marked, but its signal cannot execute outside it.
    Unfilled orders expire after their next eligible open; end orders expire explicitly.
    """
    settings = IntradaySpec.from_dict(spec)
    _check_cancelled(cancelled)
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        writers = {}
        for name, columns in _COLUMNS.items():
            stream = stack.enter_context((output_dir / f"{name}.csv").open("w", encoding="utf-8", newline=""))
            writers[name] = csv.DictWriter(stream, fieldnames=columns)
            writers[name].writeheader()
        ledger = _Ledger(settings, writers, metadata, emit, cancelled)
        for _, group in groupby(_bars(rows, _utc(settings.start, "start"), _utc(settings.end, "end"), cancelled),
                                key=lambda row: row["bar_end"]):
            ledger.process(list(group))
        if not ledger.counts["bars"]:
            raise ValueError("No completed bars in the requested backtest interval; check dataset coverage and start/end")
        ledger.finish_bar(final_day=True)
        for order in ledger.pending.values():
            ledger.finish_order(order, reason="end_of_period")
        _check_cancelled(cancelled)
        assumptions = {
            "ledger": "research_price_ledger",
            "execution": "next available real bar open after completed signal; no order-book or queue simulation",
            "participation": "previous completed signal-bar volume; remainder expires at next eligible open",
            "settlement": "supplied trading_days calendar" if ledger.calendar else "distinct observed trading dates; missing sessions are unknown",
            "cash_interest": "closing cash times annual cash_rate / 252, once per observed trading day",
            "performance": "daily closing equity returns, 252 trading days/year; intraday drawdown reported separately",
            "default_lot_size": settings.lot_size,
            "default_settlement_days": settings.settlement_days,
            "drawdown_pause": "sticky for remaining period; attempts liquidation subject to settlement and participation",
            "tradability": "positive valid OHLC bars assumed tradable; no inferred suspensions or limit states",
            "corporate_actions": "no corporate-action cash flows; adjusted prices not double counted; raw_cash unsupported",
            "limitations": metadata.get("limitations", []),
        }
        result = dict(metrics=ledger.metrics(), assumptions=assumptions, counts=ledger.counts,
                      period=dict(start=settings.start, end=settings.end, first_bar=ledger.first_time, last_bar=ledger.last_time),
                      spec=asdict(settings), metadata=metadata)
    _check_cancelled(cancelled)
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result
