import json

import pandas as pd
import pytest

from quanto.backtest.metrics import compute_metrics, max_drawdown
from quanto.backtest.models import BacktestResult, TABLE_COLUMNS
from tests.fixtures.engine_cases import bundle_case, spec_case


def result_with_equity(values):
    tables = {name: pd.DataFrame(columns=columns) for name, columns in TABLE_COLUMNS.items()}
    tables["equity"] = pd.DataFrame({
        "date": pd.bdate_range("2024-01-01", periods=len(values)),
        "cash": values, "receivables": 0, "market_value": 0,
        "equity": values, "stale_value": 0,
    })
    return BacktestResult(**tables, completed_through=tables["equity"].date.iloc[-1] if values else None)


def test_first_day_return_uses_initial_cash():
    # A 90-share fill at 10 with 1% commission costs 909; close at 11 is worth 990.
    result = result_with_equity([1081])
    spec = spec_case(bundle_case(days=1))
    metrics = compute_metrics(result, spec)
    assert metrics["total_return"] == pytest.approx(0.081, abs=1e-8)
    assert metrics["final_equity"] == 1081
    assert metrics["annual_return"] is None
    assert json.loads(json.dumps(metrics, allow_nan=False))["total_return"] == pytest.approx(0.081)


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
