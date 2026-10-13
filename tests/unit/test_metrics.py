import json
from dataclasses import replace

import pandas as pd
import pytest

from quanto.backtest.metrics import compute_metrics, max_drawdown
from quanto.backtest.models import BacktestResult, TABLE_COLUMNS
from quanto.backtest.engine import run_backtest
from tests.fixtures.engine_cases import ScriptedStrategy, bundle_case, spec_case, target


def result_with_equity(values):
    tables = {name: pd.DataFrame(columns=columns) for name, columns in TABLE_COLUMNS.items()}
    tables["equity"] = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=len(values)),
        "cash": values, "receivables": 0, "market_value": 0,
        "equity": values, "stale_value": 0,
    })
    return BacktestResult(**tables, completed_through=tables["equity"].date.iloc[-1] if values else None)


def test_first_day_return_uses_initial_cash():
    result = result_with_equity([1081])
    spec = spec_case(bundle_case(days=1))
    metrics = compute_metrics(result, spec)
    assert metrics["total_return"] == pytest.approx(0.081, abs=1e-8)
    assert metrics["final_equity"] == 1081
    assert metrics["annual_return"] is None
    assert json.loads(json.dumps(metrics, allow_nan=False))["total_return"] == pytest.approx(0.081)


def test_engine_fill_commission_flows_to_reported_return():
    bundle = bundle_case(days=2, values={(2, "A"): (10, 11)})
    spec = spec_case(bundle, execution={"commission_rate": 0.01})
    result = run_backtest(bundle, ScriptedStrategy({1: target(A=0.9)}), spec,
        lambda event: None, lambda: False)
    metrics = compute_metrics(result, spec)
    assert result.fills.iloc[0].quantity == 90
    assert result.fills.iloc[0].commission == pytest.approx(9, abs=1e-8)
    assert result.equity.iloc[-1].cash == pytest.approx(91, abs=1e-8)
    assert metrics["total_return"] == pytest.approx(0.081, abs=1e-8)


def test_cash_only_metrics_are_defined_or_null():
    result = result_with_equity([1000, 1000, 1000])
    metrics = compute_metrics(result, spec_case(bundle_case(days=3)))
    assert metrics["total_return"] == 0
    assert metrics["volatility"] == 0
    assert metrics["sharpe"] is None
    assert metrics["calmar"] is None
    assert metrics["round_trip_count"] == 0
    assert metrics["fill_count"] == 0


def test_empty_equity_is_rejected():
    with pytest.raises(ValueError, match="EMPTY_EQUITY"):
        compute_metrics(result_with_equity([]), spec_case(bundle_case(days=1)))


def test_max_drawdown_includes_initial_cash_baseline():
    assert max_drawdown(pd.Series([100, 120, 90, 110]), initial_cash=100) == pytest.approx(0.25)


def test_annualization_uses_observed_sessions_and_nominal_risk_free_rate():
    result = result_with_equity([1000, 1010])
    spec = spec_case(bundle_case(days=2), metrics={"annual_sessions": 2, "risk_free_rate": 0.02})
    metrics = compute_metrics(result, spec)
    assert metrics["annual_return"] == pytest.approx(0.01)
    assert metrics["volatility"] == pytest.approx(0.005 * 2 ** 0.5)
    assert metrics["sharpe"] == pytest.approx(-2 ** 0.5)


def test_nonfinite_equity_is_rejected():
    with pytest.raises(ValueError, match="INVALID_EQUITY"):
        compute_metrics(result_with_equity([float("nan")]), spec_case(bundle_case(days=1)))


def test_turnover_fees_exposure_and_final_unrealized_pnl():
    result = result_with_equity([1000, 1000, 1000])
    equity = result.equity.copy()
    equity["market_value"] = [0, 100, 200]
    fills = pd.DataFrame([dict(order_id="b1", trade_date="2024-01-02", symbol="A", side="buy",
        quantity=10, price=10, commission=1, slippage_cost=2, stamp_duty=0., transfer_fee=0., sellable_date="2024-01-03")])
    positions = pd.DataFrame([dict(date="2024-01-02", symbol="A", shares=10, sellable_shares=10,
        market_value=100, weight=0.1, cost_basis=101, unrealized_pnl=999, realized_pnl=0),
        dict(date="2024-01-03", symbol="A", shares=10, sellable_shares=10,
        market_value=200, weight=0.2, cost_basis=101, unrealized_pnl=99, realized_pnl=0)])
    metrics = compute_metrics(replace(result, equity=equity, fills=fills, positions=positions),
        spec_case(bundle_case(days=3)))
    assert metrics["turnover"] == pytest.approx(0.1)
    assert metrics["commission_paid"] == pytest.approx(1)
    assert metrics["slippage_cost"] == pytest.approx(2)
    assert metrics["execution_cost"] == pytest.approx(3)
    assert metrics["average_invested_ratio"] == pytest.approx(0.1)
    assert metrics["unrealized_pnl"] == pytest.approx(99)


def test_unrealized_pnl_uses_final_equity_date_even_when_positions_empty():
    result = result_with_equity([1000, 1000])
    positions = pd.DataFrame([dict(date="2024-01-01", symbol="A", shares=10, sellable_shares=10,
        market_value=100, weight=0.1, cost_basis=90, unrealized_pnl=10, realized_pnl=0)])
    metrics = compute_metrics(replace(result, positions=positions), spec_case(bundle_case(days=2)))
    assert metrics["unrealized_pnl"] == 0
