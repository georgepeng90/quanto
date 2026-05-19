import pandas as pd

from quanto.risk.market_regime import classify_market_regime


def test_market_danger_state_for_below_ma_and_low_turnover():
    benchmark = pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=3),
            "close": [100.0, 90.0, 80.0],
            "turnover": [800.0, 600.0, 600.0],
        }
    )
    regime = classify_market_regime(
        benchmark,
        {"ma_window": 2, "tradable_turnover": 850.0, "danger_turnover": 700.0, "cautious_total_weight": 0.3},
    )
    last = regime.iloc[-1]
    assert last["regime"] == "danger"
    assert last["total_weight_cap"] == 0.0
