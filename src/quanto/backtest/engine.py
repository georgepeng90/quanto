from __future__ import annotations

from dataclasses import asdict
from datetime import date
import copy
import json
import random
from typing import Callable
from collections.abc import Mapping

import numpy as np
import pandas as pd

from quanto.config import BacktestSpec
from quanto.backtest.dataset_schemas import DatasetBundle
from quanto.data.point_in_time import CORE_TABLES, TableSemantics, _bind_point_in_time
from quanto.factors.features import FeatureCache, FeatureSetDefinition, FrozenTransform
from quanto.framework import AssetBinding
from quanto.risk.policy import RiskPolicy
from quanto.strategies.base import Hold, Strategy, StrategyContext, TargetPortfolio, is_week_end, validate_intent, validate_warmup_window
from .ledger import PortfolioLedger
from .models import BacktestCancelled, BacktestResult, RunEvent, TABLE_COLUMNS, money, whole_shares
from .orders import create_orders, execute_open
from .trading import ETF_TRADING_DEFINITION, TradingDefinition


def _json_value(value):
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat() if not isinstance(value, pd.Timestamp) or value.tzinfo else value.date().isoformat()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"不可序列化的结果值: {type(value).__name__}")


class _SessionData:
    """Read-only market indexes shared by episodes of one environment/basket."""

    def __init__(self, bundle: DatasetBundle, symbols: list[str]):
        self.calendar = pd.DatetimeIndex(bundle.calendar.date).sort_values().unique()
        self.prices = bundle.prices[bundle.prices.symbol.isin(symbols)].sort_values(["date", "symbol"])
        self.instruments = bundle.instruments[bundle.instruments.symbol.isin(symbols)]
        self.groups = self.instruments.set_index("symbol")["group"].to_dict()
        self.price_groups = {d: frame for d, frame in self.prices.groupby("date")}
        self.status_groups = {d: frame.set_index("symbol").to_dict("index") for d, frame in bundle.status.groupby("date")}
        self.eligibility_groups = {d: set(frame.loc[frame.eligible, "symbol"]) for d, frame in bundle.eligibility.groupby("date")}
        self._quotes = {}
        self.market_views = {}

    def quotes(self, asof):
        if asof not in self._quotes:
            daily = self.price_groups.get(asof, self.prices.iloc[:0]).set_index("symbol")
            self._quotes[asof] = daily.open.to_dict(), daily.close.to_dict()
        return self._quotes[asof]


class SessionRunner:
    """Pause the shared daily account at close to receive one causal intent."""

    def __init__(self, bundle: DatasetBundle, spec: BacktestSpec,
                 cancelled: Callable[[], bool], collect: bool = True, *, prepared: _SessionData | None = None,
                 trading_definition: TradingDefinition = ETF_TRADING_DEFINITION,
                 asset_binding: AssetBinding | None = None, feature_definition: FeatureSetDefinition | None = None,
                 feature_params: Mapping[str, object] | None = None, feature_transform: FrozenTransform | None = None,
                 table_semantics: tuple[TableSemantics, ...] | None = None, feature_cache: FeatureCache | None = None):
        spec.validate_for_dataset(bundle)
        if bundle.price_mode not in trading_definition.supported_price_modes:
            raise ValueError("EXECUTION_CAPABILITY_MISSING: unsupported price mode")
        if asset_binding is not None:
            mapped = {asset.local_symbol: asset for asset in asset_binding.assets}
            if not set(spec.symbols).issubset(mapped):
                raise ValueError("BINDING_MISMATCH: run symbols absent from binding")
            types = {mapped[symbol].instrument_id.instrument_type for symbol in spec.symbols}
            if len(types) > 1:
                raise ValueError("MIXED_BASKET_UNSUPPORTED: one instrument type per run")
            if not types.issubset(trading_definition.supported_instrument_types):
                raise ValueError("EXECUTION_CAPABILITY_MISSING: trading definition does not support basket")
            if feature_definition is not None and not types.issubset(feature_definition.supported_instrument_types):
                raise ValueError("DEFINITION_UNAVAILABLE: feature definition does not support basket")
        elif feature_definition is not None:
            raise ValueError("IDENTITY_UNRESOLVED: shared features require an asset binding")
        self.bundle, self.spec = bundle, spec
        self.trading_definition, self.asset_binding = trading_definition, asset_binding
        self.feature_definition, self.feature_params = feature_definition, dict(feature_params or {})
        self.feature_transform, self.table_semantics = feature_transform, table_semantics
        self.feature_cache = feature_cache if feature_cache is not None else FeatureCache()
        self.cancelled, self.collect = cancelled, collect
        self._prepared = prepared
        calendar = prepared.calendar if prepared is not None else pd.DatetimeIndex(bundle.calendar.date).sort_values().unique()
        self.dates = calendar[(calendar >= pd.Timestamp(spec.start)) & (calendar <= pd.Timestamp(spec.end))]
        self.completed_through = None
        self.context = None
        self._tables = {name: [] for name in TABLE_COLUMNS}
        self._orders = []
        self._phase = "completed"
        self._sessions = self._run_sessions()

    def advance_to_close(self) -> StrategyContext:
        if self._phase == "close":
            raise RuntimeError("Submit the current close intent before advancing")
        if self.cancelled():
            raise BacktestCancelled("CANCELLED: 回测已取消")
        context = next(self._sessions)
        self._phase = "close"
        return context

    def submit_close_intent(self, intent: Hold | TargetPortfolio) -> RunEvent:
        if self._phase != "close":
            raise RuntimeError("Advance to close before submitting an intent")
        if self.cancelled():
            raise BacktestCancelled("CANCELLED: 回测已取消")
        validate_intent(intent, set(self.spec.symbols))
        event = self._sessions.send(intent)
        self._phase = "completed"
        return event

    def result(self) -> BacktestResult:
        tables = self._tables if self.collect else {name: [] for name in TABLE_COLUMNS}
        return BacktestResult(**{name: pd.DataFrame(
            [asdict(order) for order in self._orders] if name == "orders" and self.collect else rows,
            columns=TABLE_COLUMNS[name]) for name, rows in tables.items()},
            completed_through=self.completed_through)

    def _run_sessions(self):
        bundle, spec, cancelled = self.bundle, self.spec, self.cancelled
        spec.validate_for_dataset(bundle)
        if self._prepared is None:
            self._prepared = _SessionData(bundle, spec.symbols)
        prepared = self._prepared
        calendar = prepared.calendar
        dates = calendar[(calendar >= pd.Timestamp(spec.start)) & (calendar <= pd.Timestamp(spec.end))]
        prices = prepared.prices
        ledger, risk = PortfolioLedger(spec.initial_cash), RiskPolicy(spec.risk, initial_equity=spec.initial_cash)
        ledger.calendar = calendar
        self.ledger, self.risk = ledger, risk
        instruments, groups = prepared.instruments, prepared.groups
        instrument_rows = instruments.set_index("symbol").to_dict("index")
        instrument_rows = {symbol: instrument_rows[symbol] for symbol in spec.symbols}
        if self.asset_binding is not None:
            identities = {asset.local_symbol: asset.instrument_id for asset in self.asset_binding.assets}
            for symbol, row in instrument_rows.items():
                row["instrument_id"] = identities[symbol].model_dump(mode="json")
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
        if not corporate.empty:
            event_dates = corporate[["record_date", "ex_date", "pay_date"]].apply(pd.to_datetime)
            in_interval = event_dates.apply(lambda column: column.between(pd.Timestamp(spec.start), pd.Timestamp(spec.end))).any(axis=1)
            corporate = corporate.loc[in_interval | event_dates.isna().all(axis=1)]
        corporate = pd.DataFrame(self.trading_definition.events(corporate, price_mode=bundle.price_mode), columns=corporate.columns)
        entitlements = {}
        orders, tables, previous_orders = [], {name: [] for name in TABLE_COLUMNS}, {}
        self._tables, self._orders = tables, orders
        status_groups, eligibility_groups = prepared.status_groups, prepared.eligibility_groups

        for day_number, asof in enumerate(dates, 1):
            if cancelled():
                raise BacktestCancelled("CANCELLED: 回测已取消")
            if not self.collect:
                for rows in tables.values():
                    rows.clear()
                orders[:] = [order for order in orders if order.status == "pending"]
                previous_orders.clear()
            offsets = {name: len(rows) for name, rows in tables.items()}
            status = status_groups.get(asof, {})
            for symbol, row in instrument_rows.items():
                if pd.Timestamp(row["listing_date"]) <= asof:
                    self.trading_definition.eligibility(status.get(symbol, {}))

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
            opens, closes = prepared.quotes(asof)
            scheduled = [order for order in orders if order.status == "pending" and order.execution_date == asof]
            fills = execute_open(scheduled, opens, status, ledger, spec.execution, trading_definition=self.trading_definition)
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
            data_view, features = None, None
            if self.asset_binding is not None:
                key = (self.asset_binding.binding_hash, tuple(spec.symbols), cutoff)
                if key not in prepared.market_views:
                    market_tables = {**{name: getattr(bundle, name) for name in CORE_TABLES}, **bundle.auxiliary}
                    prepared.market_views[key] = _bind_point_in_time(market_tables, self.asset_binding, tuple(spec.symbols),
                        cutoff, semantics=self.table_semantics)
                data_view = prepared.market_views[key]
                if self.feature_definition is not None:
                    features = self.feature_cache.get_or_compute(self.feature_definition, data_view, self.feature_params, self.feature_transform)
            ctx = StrategyContext(asof, prices, eligible, portfolio,
                tuple(asdict(order) for order in orders if order.status == "pending"),
                {**risk.state, "symbol_groups": groups, "risk_approvals": known_approvals}, is_week_end(calendar, asof),
                data_view=data_view, features=features, instruments=instrument_rows)
            self.context = ctx
            proposed = yield ctx
            decision = risk.evaluate(ctx, proposed)
            tables["risk_events"].extend(decision.events)
            intent = decision.intent
            tables["signals"].append(copy.deepcopy(dict(date=asof, intent="hold" if isinstance(proposed, Hold) else "target",
                weights={} if isinstance(proposed, Hold) else proposed.weights,
                reasons={"hold": proposed.reason} if isinstance(proposed, Hold) else proposed.reasons,
                diagnostics={} if isinstance(proposed, Hold) else proposed.diagnostics,
                approved_intent="hold" if isinstance(intent, Hold) else "target",
                approved_weights={} if isinstance(intent, Hold) else intent.weights,
                approved_reasons={"hold": intent.reason} if isinstance(intent, Hold) else intent.reasons,
                approved_diagnostics={} if isinstance(intent, Hold) else intent.diagnostics)))
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
            self.completed_through = asof
            updates = []
            for order in orders:
                row = asdict(order)
                if previous_orders.get(order.id) != row:
                    updates.append(row)
                    previous_orders[order.id] = row
            delta = {name: rows[offsets[name]:] for name, rows in tables.items() if name != "orders"}
            delta["orders"] = updates
            payload = json.loads(json.dumps(dict(tables=delta, completed_through=asof.date().isoformat()), default=_json_value, allow_nan=False))
            yield RunEvent(day_number, "backtest", asof.date().isoformat(), day_number, len(dates), "info", "DAY_COMPLETED", "交易日计算完成", payload)


def run_backtest(
    bundle: DatasetBundle,
    strategy: Strategy,
    spec: BacktestSpec,
    emit: Callable[[RunEvent], None],
    cancelled: Callable[[], bool],
    *, trading_definition: TradingDefinition = ETF_TRADING_DEFINITION, asset_binding: AssetBinding | None = None,
    feature_definition: FeatureSetDefinition | None = None, feature_params: Mapping[str, object] | None = None,
    feature_transform: FrozenTransform | None = None, table_semantics: tuple[TableSemantics, ...] | None = None,
) -> BacktestResult:
    """Complete each session atomically at emit; no filesystem output."""
    spec.validate_for_dataset(bundle)
    observed = pd.DatetimeIndex(bundle.prices.loc[bundle.prices.symbol.isin(spec.symbols), "date"].unique()).sort_values()
    validate_warmup_window(strategy, observed, spec.start, spec.end)
    random.seed(spec.seed)
    np.random.seed(spec.seed % 2**32)
    runner = SessionRunner(bundle, spec, cancelled, trading_definition=trading_definition, asset_binding=asset_binding,
        feature_definition=feature_definition, feature_params=feature_params, feature_transform=feature_transform,
        table_semantics=table_semantics)
    evaluated = False
    for _ in runner.dates:
        context = runner.advance_to_close()
        proposed = Hold("预热或非评估日")
        if observed.searchsorted(context.asof, side="left") >= strategy.warmup_bars and strategy.should_evaluate(context):
            evaluated = True
            proposed = strategy.on_close(context)
        emit(runner.submit_close_intent(proposed))
    if not evaluated:
        raise ValueError("NO_EVALUATION: 预热后没有可评估的策略交易日")
    return runner.result()
