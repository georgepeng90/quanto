import pandas as pd

from quanto.backtest.engine import _build_targets
from quanto.factors import compute_factor_panel, score_factors


def _synthetic_prices() -> pd.DataFrame:
    rows = []
    for symbol, drift in [("A", 0.3), ("B", 0.2), ("C", 0.1), ("D", 0.0)]:
        for i, date in enumerate(pd.bdate_range("2024-01-01", periods=90)):
            close = 100 + drift * i
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1_000_000 + i,
                    "amount": 100_000_000,
                }
            )
    return pd.DataFrame(rows)


def test_weekly_targets_execute_after_signal_date():
    config = {
        "momentum": {"enabled": True, "weight": 0.4, "windows": [20, 60]},
        "low_volatility": {"enabled": True, "weight": 0.25, "window": 20},
        "volume_confirmation": {"enabled": True, "weight": 0.2, "short_window": 5, "long_window": 20},
        "normalize_active_weights": True,
    }
    prices = _synthetic_prices()
    scores = score_factors(compute_factor_panel(prices, config), config)
    targets = _build_targets(scores, prices, top_n=3)
    assert not targets.empty
    assert (targets["trade_date"] > targets["signal_date"]).all()
