import pandas as pd
import pytest

from quanto.backtest.metrics import compute_metrics, max_drawdown


def test_max_drawdown_and_metrics_are_computed():
    equity = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=4),
            "equity": [100.0, 120.0, 90.0, 110.0],
        }
    )
    assert max_drawdown(equity["equity"]) == 0.25
    metrics = compute_metrics(equity)
    assert metrics["total_return"] == pytest.approx(0.1)
    assert metrics["max_drawdown"] == 0.25
    assert "sharpe" in metrics
