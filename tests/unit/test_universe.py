import pandas as pd

from quanto.universe import build_eligibility


def test_eligibility_does_not_delete_prices():
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

    instruments = pd.DataFrame({"symbol": ["512480"], "lot_size": [100], "settlement_sessions": [1], "listing_date": ["2024-01-01"], "group": ["industry"]})
    eligible = build_eligibility(prices, instruments, {"min_history_days": 3, "min_avg_turnover_20d": 0})

    assert prices.shape[0] == 5
    assert eligible.loc[:2, "eligible"].tolist() == [False, False, True]
    assert eligible.shape[0] == 5
