import pandas as pd
import pytest
from quanto.backtest.ledger import PortfolioLedger
from quanto.backtest.models import Fill
from quanto.backtest.orders import create_orders, execute_open
from tests.fixtures.engine_cases import bundle_case, spec_case, target


def trading_status(*, can_buy=True, can_sell=True):
    """Observed fixture status for the A/B allocation tests."""
    return {symbol: dict(status="trading", can_buy=can_buy, can_sell=can_sell) for symbol in ("A", "B")}


def fill(side, quantity, price=10, day=1, commission=0):
    return Fill("fixture", pd.Timestamp(f"2024-01-{day:02}"), "A", side, quantity, price, commission, 0)


def orders_for(ledger, intent, b, prices=None):
    return create_orders(intent, ledger.snapshot(pd.Timestamp("2024-01-02")), prices or {"A": 10, "B": 10},
        pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02"), b.instruments)


def test_sell_before_buy_is_order_invariant():
    for reverse in (False, True):
        ledger = PortfolioLedger(1000)
        ledger.apply_fill(fill("buy", 100))
        ledger.mark(pd.Timestamp("2024-01-01"), {"A": 10}, {}, 0)
        b = bundle_case(symbols=("A", "B"))
        orders = orders_for(ledger, target(B=1), b)
        if reverse:
            orders.reverse()
        execute_open(orders, {"A": 10, "B": 10}, trading_status(), ledger, spec_case(b).execution)
        final = ledger.snapshot(pd.Timestamp("2024-01-02"))
        assert final.shares == {"A": 0, "B": 100}
        assert final.cash == pytest.approx(0, abs=1e-8)


def test_suspended_holding_keeps_last_mark():
    ledger = PortfolioLedger(1500)
    ledger.apply_fill(fill("buy", 100))
    ledger.mark(pd.Timestamp("2024-01-01"), {"A": 10}, {}, 2)
    ledger.mark(pd.Timestamp("2024-01-02"), {}, {"A": {"status": "suspended"}}, 2)
    suspended = ledger.snapshot(pd.Timestamp("2024-01-02"))
    assert suspended.equity == 1500 and suspended.stale_value == 1000
    with pytest.raises(ValueError, match="MISSING_PRICE"):
        ledger.mark(pd.Timestamp("2024-01-03"), {}, {}, 2)


def test_fifo_basis_fees_and_split():
    ledger = PortfolioLedger(1000)
    ledger.apply_fill(fill("buy", 10, 10, commission=1))
    ledger.apply_fill(fill("buy", 10, 12, day=2, commission=1))
    ledger.apply_fill(fill("sell", 15, 11, day=3, commission=1))
    assert ledger.realized_pnl == pytest.approx(2.5, abs=1e-8)
    assert ledger.cost_basis("A") == pytest.approx(60.5, abs=1e-8)
    ledger.apply_event(dict(event_type="split", symbol="A", split_ratio=2))
    assert ledger.snapshot(pd.Timestamp("2024-01-04")).shares["A"] == 10
    assert ledger.cost_basis("A") == pytest.approx(60.5, abs=1e-8)


def test_sell_failure_does_not_prefund_buy_and_fees_reduce_quantity():
    ledger = PortfolioLedger(1000)
    ledger.apply_fill(fill("buy", 100))
    ledger.mark(pd.Timestamp("2024-01-01"), {"A": 10}, {}, 0)
    b = bundle_case(symbols=("A", "B"))
    orders = orders_for(ledger, target(B=1), b)
    assert execute_open(orders, {"A": 10, "B": 10}, trading_status(can_sell=False), ledger, spec_case(b).execution) == []
    assert all(o.status == "expired" for o in orders)
    ledger = PortfolioLedger(1000)
    orders = orders_for(ledger, target(A=1), b)
    fills = execute_open(orders, {"A": 10}, trading_status(), ledger, spec_case(b, execution={"minimum_commission": 5}).execution)
    assert fills[0].quantity == 99 and ledger.cash == 5
    assert orders[0].quantity == 100 and orders[0].filled_quantity == 99 and orders[0].expired_quantity == 1


def test_full_exit_sells_integer_odd_lot_after_split():
    b = bundle_case()
    b.instruments.loc[0, "lot_size"] = 100
    ledger = PortfolioLedger(1000)
    ledger.apply_fill(fill("buy", 100))
    ledger.apply_event(dict(event_type="split", symbol="A", split_ratio=1.5))
    ledger.mark(pd.Timestamp("2024-01-01"), {"A": 10 / 1.5}, {}, 0)
    orders = orders_for(ledger, target(), b, {"A": 10 / 1.5})
    assert orders[0].quantity == 150
    execute_open(orders, {"A": 10 / 1.5}, trading_status(), ledger, spec_case(b).execution)
    assert ledger.snapshot(pd.Timestamp("2024-01-02")).shares["A"] == 0


def test_partial_settlement_expires_remainder_without_overselling():
    b = bundle_case()
    ledger = PortfolioLedger(1000)
    ledger.apply_fill(fill("buy", 40))
    ledger.apply_fill(fill("buy", 60, day=2))
    ledger.mark(pd.Timestamp("2024-01-02"), {"A": 10}, {}, 0)
    orders = orders_for(ledger, target(), b)
    orders[0].source, orders[0].reason = "risk", "risk_pause"
    fills = execute_open(orders, {"A": 10}, trading_status(), ledger, spec_case(b).execution)
    assert fills[0].quantity == 40
    assert orders[0].expired_quantity == 60
    assert orders[0].reason == "insufficient_sellable"
    assert ledger.snapshot(pd.Timestamp("2024-01-03")).shares["A"] == 60


def test_buy_allocation_is_proportional_and_deterministic_after_gap():
    b = bundle_case(symbols=("A", "B"))
    for reverse in (False, True):
        ledger = PortfolioLedger(1000)
        orders = orders_for(ledger, target(A=.5, B=.5), b)
        if reverse:
            orders.reverse()
        fills = execute_open(orders, {"A": 12, "B": 12}, trading_status(), ledger, spec_case(b).execution)
        assert {f.symbol: f.quantity for f in fills} == {"A": 42, "B": 41}
        assert ledger.cash == 4


def test_fractional_split_rejected_and_stale_session_limit_enforced():
    ledger = PortfolioLedger(100)
    ledger.apply_fill(fill("buy", 1))
    with pytest.raises(ValueError, match="FRACTIONAL_SHARES"):
        ledger.apply_event(dict(event_type="split", symbol="A", split_ratio=1.5))
    ledger.mark(pd.Timestamp("2024-01-01"), {"A": 10}, {}, 1)
    ledger.mark(pd.Timestamp("2024-01-02"), {}, {"A": {"status": "suspended"}}, 1)
    with pytest.raises(ValueError, match="STALE_PRICE_LIMIT"):
        ledger.mark(pd.Timestamp("2024-01-03"), {}, {"A": {"status": "suspended"}}, 1)


def test_expired_orders_never_retry_and_side_bans_apply():
    b = bundle_case()
    ledger = PortfolioLedger(1000)
    orders = orders_for(ledger, target(A=1), b)
    assert execute_open(orders, {"A": 10}, trading_status(can_buy=False), ledger, spec_case(b).execution) == []
    assert orders[0].reason == "cannot_buy"
    assert execute_open(orders, {"A": 10}, trading_status(), ledger, spec_case(b).execution) == []


def test_favorable_gap_cannot_enlarge_request_and_slippage_is_auditable():
    b = bundle_case()
    ledger = PortfolioLedger(1000)
    orders = orders_for(ledger, target(A=1), b)
    fills = execute_open(orders, {"A": 8}, trading_status(), ledger, spec_case(b, execution={"slippage_rate": .01}).execution)
    assert fills[0].quantity == 100
    assert fills[0].price == pytest.approx(8.08, abs=1e-8)
    assert fills[0].slippage_cost == pytest.approx(8, abs=1e-8)
    assert ledger.cash == pytest.approx(192, abs=1e-8)


def test_actual_cap_reduction_after_odd_lot_split_sells_one_legal_lot():
    from quanto.risk.policy import RiskPolicy
    from quanto.strategies.base import Hold, StrategyContext

    b = bundle_case()
    b.instruments.loc[0, "lot_size"] = 100
    ledger = PortfolioLedger(1000)
    ledger.apply_fill(fill("buy", 100))
    ledger.apply_event(dict(event_type="split", symbol="A", split_ratio=1.5))
    ledger.mark(pd.Timestamp("2024-01-01"), {"A": 10 / 1.5}, {}, 0)
    ctx = StrategyContext(pd.Timestamp("2024-01-02"), b.prices, ("A",), ledger.snapshot(pd.Timestamp("2024-01-02")), (), {}, False)
    decision = RiskPolicy(spec_case(b, risk={"max_symbol_weight": .8}).risk).evaluate(ctx, Hold("保持"))
    orders = orders_for(ledger, decision.intent, b, {"A": 10 / 1.5})
    assert len(orders) == 1 and orders[0].quantity == 100
    assert not orders[0].full_exit
    execute_open(orders, {"A": 10 / 1.5}, trading_status(), ledger, spec_case(b).execution)
    portfolio = ledger.snapshot(pd.Timestamp("2024-01-02"))
    assert portfolio.shares["A"] == 50
    assert portfolio.market_values["A"] / portfolio.equity <= .8


def test_reserved_interest_cash_cannot_be_spent_before_close():
    b = bundle_case()
    ledger = PortfolioLedger(990)
    orders = orders_for(ledger, target(A=1), b)
    ledger.reserve_cash(9.9)
    fills = execute_open(orders, {"A": 10}, trading_status(), ledger, spec_case(b).execution)
    assert fills[0].quantity == 98
    assert ledger.cash == 10 and ledger.available_cash == pytest.approx(.1, abs=1e-8)
    ledger.apply_event(dict(event_type="interest", amount=-9.9))
    assert ledger.cash == pytest.approx(.1, abs=1e-8)
    assert ledger.available_cash == ledger.cash


def test_ledger_rejects_unfunded_interest_without_mutating_cash():
    ledger = PortfolioLedger(1000)
    with pytest.raises(ValueError, match="UNFUNDED_INTEREST"):
        ledger.apply_event(dict(event_type="interest", amount=-1001))
    assert ledger.cash == 1000
