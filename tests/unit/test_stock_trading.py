from dataclasses import asdict
from datetime import date

import pandas as pd
import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.ledger import PortfolioLedger
from quanto.backtest.metrics import compute_metrics
from quanto.backtest.models import BacktestResult, Order, TABLE_COLUMNS
from quanto.backtest.orders import execute_open
from quanto.backtest.stock_data import validate_stock_data
from quanto.backtest.stock_trading import STOCK_TRADING_DEFINITION
from quanto.config import BacktestSpec
from quanto.framework import ResearchInterval
from tests.fixtures.stock_training_cases import stock_case, stock_binding


@pytest.fixture
def setup(tmp_path):
    raw, dates = stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    spec = BacktestSpec.model_validate(raw["backtest"])
    context = validate_stock_data(bundle, stock_binding(bundle), ("000001",),
        ResearchInterval(start=spec.start, end=spec.end, warmup_start=spec.start))
    return spec, context


def trade(ledger, spec, context, side, quantity, price, day, multiplier=1):
    order = Order(f"{day}-{side}", "000001", side, quantity, pd.Timestamp(day), pd.Timestamp(day), lot_size=100)
    execution = spec.execution.model_copy(update={"minimum_commission": 5 * multiplier, "commission_rate": .0003 * multiplier, "slippage_rate": 0})
    state = {"000001": dict(context.open_status["000001", day])}
    fills = execute_open([order], {"000001": price}, state, ledger, execution,
        trading_definition=STOCK_TRADING_DEFINITION.with_cost_multiplier(multiplier), stock_context=context)
    return fills


@pytest.mark.parametrize("multiplier,expected_cash,expected_fee", [(1, 2189.38, 10.62), (2, 2178.76, 21.24)])
def test_stock_fees_cash_fifo_and_report_match_hand_calculation(setup, multiplier, expected_cash, expected_fee):
    spec, context = setup
    ledger = PortfolioLedger(2000)
    buy_day, sell_day = date(2024, 1, 15), date(2024, 1, 16)
    fills = trade(ledger, spec, context, "buy", 100, 10, buy_day, multiplier)
    equity = [ledger.mark(buy_day, {"000001": 10}, {}, 0)]
    fills += trade(ledger, spec, context, "sell", 100, 12, sell_day, multiplier)
    equity += [ledger.mark(sell_day, {"000001": 12}, {}, 0)]
    assert ledger.available_cash == expected_cash
    assert ledger.realized_pnl == pytest.approx(expected_cash - 2000)
    assert sum(fill.total_fees for fill in fills) == pytest.approx(expected_fee)
    frames = {name: pd.DataFrame(columns=columns) for name, columns in TABLE_COLUMNS.items()}
    frames.update(equity=pd.DataFrame(equity), fills=pd.DataFrame([asdict(fill) for fill in fills]))
    result = BacktestResult(**frames, completed_through=pd.Timestamp(sell_day))
    metrics = compute_metrics(result, spec.model_copy(update={"initial_cash": 2000}))
    assert metrics["stamp_duty_paid"] == pytest.approx(.6 * multiplier)
    assert metrics["transfer_fee_paid"] == pytest.approx(.02 * multiplier)
    assert metrics["commission_paid"] == 10 * multiplier
    assert metrics["execution_cost"] == pytest.approx(expected_fee)
    assert metrics["realized_pnl"] == pytest.approx(ledger.realized_pnl)


@pytest.mark.parametrize("cash,quantity", [(1005, 0), (1005.01, 100), (2005.01, 100), (2005.02, 200)])
def test_buy_budget_includes_all_fees(setup, cash, quantity):
    spec, context = setup
    ledger = PortfolioLedger(cash)
    fills = trade(ledger, spec, context, "buy", 200, 10, date(2024, 1, 15))
    assert sum(fill.quantity for fill in fills) == quantity
    assert ledger.available_cash >= 0


def test_partial_fifo_and_t_plus_one(setup):
    spec, context = setup
    ledger = PortfolioLedger(5000)
    trade(ledger, spec, context, "buy", 100, 10, date(2024, 1, 15))
    assert not trade(ledger, spec, context, "sell", 100, 12, date(2024, 1, 15))
    trade(ledger, spec, context, "buy", 200, 11, date(2024, 1, 16))
    trade(ledger, spec, context, "sell", 200, 12, date(2024, 1, 17))
    assert ledger.cost_basis("000001") == pytest.approx(1102.51)
    assert ledger.realized_pnl == pytest.approx(286.26)


def test_stock_execution_requires_context_and_cannot_use_close_permissions(setup):
    spec, context = setup
    with pytest.raises(ValueError, match="stock.*context"):
        execute_open([], {}, {}, PortfolioLedger(1000), spec.execution, trading_definition=STOCK_TRADING_DEFINITION)
    # The only opening permission is the frozen opening status, even if caller supplies different close flags.
    from dataclasses import replace
    statuses = dict(context.open_status)
    statuses["000001", date(2024, 1, 15)] = dict(status="trading", can_buy=False, can_sell=True)
    blocked = replace(context, open_status=statuses)
    assert not trade(PortfolioLedger(2000), spec, blocked, "buy", 100, 10, date(2024, 1, 15))


def test_fee_contract_requires_identity_date_side_and_valid_rule(setup):
    _, context = setup
    values = dict(trade_date=date(2024, 1, 15), instrument_id="SZSE:stock:000001", board="main_board", side="sell",
                  quantity=100, price=12., commission_rate=.0003, minimum_commission=5.,
                  fee_rule=context.fee_rule("000001", date(2024, 1, 15), "sell"))
    assert STOCK_TRADING_DEFINITION.fees(values) == dict(commission=5., stamp_duty=.6, transfer_fee=.01)
    assert all(v == 0 for v in STOCK_TRADING_DEFINITION.fees({**values, "quantity": 0}).values())
    for patch in ({"trade_date": date(2025, 1, 1)}, {"side": "buy"}, {"instrument_id": "SSE:stock:000001"},
                  {"quantity": -1}, {"price": float("nan")}, {"fee_rule": {}}):
        with pytest.raises(ValueError):
            STOCK_TRADING_DEFINITION.fees({**values, **patch})
    with pytest.raises(ValueError):
        STOCK_TRADING_DEFINITION.with_cost_multiplier(float("nan"))


def test_session_runner_uses_opening_permission_and_requires_evidence(tmp_path):
    from quanto.backtest.engine import SessionRunner
    from quanto.strategies.base import TargetPortfolio
    raw, dates = stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    spec = BacktestSpec.model_validate(raw["backtest"])
    # Close says buyable; the next opening says otherwise.
    frame = bundle.auxiliary["stock_open_status"]
    frame.loc[frame.index == 11, "can_buy"] = False
    runner = SessionRunner(bundle, spec, lambda: False, trading_definition=STOCK_TRADING_DEFINITION,
                           asset_binding=stock_binding(bundle))
    runner.advance_to_close()
    runner.submit_close_intent(TargetPortfolio({"000001": 1.}, {}, {}))
    runner.advance_to_close()
    assert runner.ledger.snapshot(dates[11]).shares == {}
    del bundle.auxiliary["fee_rules"]
    with pytest.raises(ValueError, match="DATA_REQUIREMENT_UNSATISFIED"):
        SessionRunner(bundle, spec, lambda: False, trading_definition=STOCK_TRADING_DEFINITION,
                       asset_binding=runner.asset_binding)
