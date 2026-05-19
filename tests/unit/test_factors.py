import pandas as pd

from quanto.factors.price_volume import compute_factor_panel
from quanto.factors.scoring import score_factors


def _prices(close_tail: float) -> pd.DataFrame:
    rows = []
    for symbol, offset in [("A", 0.0), ("B", 10.0)]:
        for i in range(70):
            close = 100 + offset + i
            if symbol == "A" and i == 69:
                close = close_tail
            rows.append(
                {
                    "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i),
                    "symbol": symbol,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1000 + i,
                    "amount": 100_000_000,
                }
            )
    return pd.DataFrame(rows)


def test_factor_values_do_not_change_when_future_price_changes():
    config = {
        "momentum": {"enabled": True, "weight": 0.4, "windows": [20, 60]},
        "low_volatility": {"enabled": True, "weight": 0.25, "window": 20},
        "volume_confirmation": {"enabled": True, "weight": 0.2, "short_window": 5, "long_window": 20},
        "normalize_active_weights": True,
    }
    original = compute_factor_panel(_prices(close_tail=169), config)
    revised = compute_factor_panel(_prices(close_tail=999), config)
    date = pd.Timestamp("2024-03-09")
    left = original[(original["signal_date"] == date) & (original["symbol"] == "A")]
    right = revised[(revised["signal_date"] == date) & (revised["symbol"] == "A")]
    pd.testing.assert_series_equal(
        left[["momentum", "low_volatility", "volume_confirmation"]].iloc[0],
        right[["momentum", "low_volatility", "volume_confirmation"]].iloc[0],
        check_names=False,
    )


def test_cross_section_scoring_uses_only_current_date_universe():
    factors = pd.DataFrame(
        [
            {"signal_date": "2024-01-05", "symbol": "A", "momentum": 1, "low_volatility": 1, "volume_confirmation": 1},
            {"signal_date": "2024-01-05", "symbol": "B", "momentum": 2, "low_volatility": 2, "volume_confirmation": 2},
            {"signal_date": "2024-01-12", "symbol": "A", "momentum": 100, "low_volatility": 100, "volume_confirmation": 100},
        ]
    )
    config = {
        "momentum": {"enabled": True, "weight": 1},
        "low_volatility": {"enabled": False, "weight": 0},
        "volume_confirmation": {"enabled": False, "weight": 0},
        "normalize_active_weights": True,
    }
    scored = score_factors(factors, config)
    first_date = scored[scored["signal_date"] == "2024-01-05"].sort_values("symbol")
    assert list(first_date["rank"]) == [2, 1]
