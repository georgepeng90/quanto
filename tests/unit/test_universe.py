import pandas as pd

from quanto.universe import Universe, apply_universe_filters


def test_universe_filters_history_by_asof_row_not_full_sample():
    prices = pd.DataFrame(
        {
            "date": pd.bdate_range("2024-01-01", periods=5),
            "symbol": ["512480"] * 5,
            "open": [1.0] * 5,
            "high": [1.0] * 5,
            "low": [1.0] * 5,
            "close": [1.0] * 5,
            "volume": [1_000_000] * 5,
            "amount": [100_000_000] * 5,
        }
    )

    filtered = apply_universe_filters(prices, Universe(["512480"], min_history_days=3, min_avg_turnover_20d=0))

    assert list(filtered["date"]) == list(pd.bdate_range("2024-01-03", periods=3))
