import pandas as pd

from quanto.risk.position_sizing import size_positions


def test_position_sizing_enforces_symbol_cap():
    targets = pd.DataFrame(
        [
            {"trade_date": pd.Timestamp("2024-01-08"), "symbol": "A", "strategy_weight": 0.5},
            {"trade_date": pd.Timestamp("2024-01-08"), "symbol": "B", "strategy_weight": 0.5},
        ]
    )
    vol = pd.DataFrame(
        [
            {"date": pd.Timestamp("2024-01-08"), "symbol": "A", "vol_20d": 0.001},
            {"date": pd.Timestamp("2024-01-08"), "symbol": "B", "vol_20d": 0.001},
        ]
    )
    regime = pd.DataFrame([{"date": pd.Timestamp("2024-01-08"), "regime": "tradable", "total_weight_cap": 1.0}])
    sized = size_positions(
        targets,
        vol,
        regime,
        {
            "max_symbol_weight": 0.15,
            "single_symbol_risk_limit": 0.005,
            "portfolio_var_limit": 0.02,
            "var_confidence_multiplier": 2.33,
        },
    )
    assert sized["target_weight"].max() <= 0.15


def test_position_sizing_zeroes_targets_in_danger_regime():
    targets = pd.DataFrame([{"trade_date": pd.Timestamp("2024-01-08"), "symbol": "A", "strategy_weight": 0.5}])
    vol = pd.DataFrame([{"date": pd.Timestamp("2024-01-08"), "symbol": "A", "vol_20d": 0.01}])
    regime = pd.DataFrame([{"date": pd.Timestamp("2024-01-08"), "regime": "danger", "total_weight_cap": 0.0}])
    sized = size_positions(
        targets,
        vol,
        regime,
        {
            "max_symbol_weight": 0.15,
            "single_symbol_risk_limit": 0.005,
            "portfolio_var_limit": 0.02,
            "var_confidence_multiplier": 2.33,
        },
    )
    assert sized["target_weight"].sum() == 0.0


def test_position_sizing_uses_signal_date_not_trade_date_for_risk_inputs():
    targets = pd.DataFrame(
        [
            {
                "signal_date": pd.Timestamp("2024-01-05"),
                "trade_date": pd.Timestamp("2024-01-08"),
                "symbol": "A",
                "strategy_weight": 1.0,
            }
        ]
    )
    vol = pd.DataFrame(
        [
            {"date": pd.Timestamp("2024-01-05"), "symbol": "A", "vol_20d": 0.01},
            {"date": pd.Timestamp("2024-01-08"), "symbol": "A", "vol_20d": 0.50},
        ]
    )
    regime = pd.DataFrame(
        [
            {"date": pd.Timestamp("2024-01-05"), "regime": "tradable", "total_weight_cap": 1.0},
            {"date": pd.Timestamp("2024-01-08"), "regime": "danger", "total_weight_cap": 0.0},
        ]
    )
    sized = size_positions(
        targets,
        vol,
        regime,
        {
            "max_symbol_weight": 0.15,
            "single_symbol_risk_limit": 0.50,
            "portfolio_var_limit": 0.50,
            "var_confidence_multiplier": 2.33,
        },
    )
    assert sized.iloc[0]["regime"] == "tradable"
    assert sized.iloc[0]["target_weight"] == 0.15
