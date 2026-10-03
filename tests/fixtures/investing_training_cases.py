import json

import numpy as np
import pandas as pd

from tests.fixtures.backtest_cases import dataset_case


def investing_case(root):
    dataset_case(root)
    dates = pd.bdate_range("2024-01-01", periods=100)
    close = 10 * np.exp(np.arange(100) * .002 + np.sin(np.arange(100)) * .003)
    pd.DataFrame(dict(date=dates, symbol="000001", open=close * .999,
                      high=close * 1.01, low=close * .99, close=close,
                      volume=100000, amount=100000000)).to_csv(root / "prices.csv", index=False)
    pd.DataFrame(dict(date=dates, close=4000 + np.arange(100), turnover=1e9)).to_csv(root / "benchmark.csv", index=False)
    pd.DataFrame(dict(date=dates)).to_csv(root / "calendar.csv", index=False)
    pd.DataFrame(dict(date=dates, symbol="000001", status="trading", can_buy=True, can_sell=True)).to_csv(root / "status.csv", index=False)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest.update(calendar_end=str(dates[-1].date()), data_end=str(dates[-1].date()))
    (root / "manifest.json").write_text(json.dumps(manifest))
    backtest = dict(start=str(dates[10].date()), end=str(dates[89].date()), symbols=["000001"],
                    initial_cash=100000., seed=42,
                    execution=dict(commission_rate=.0003, minimum_commission=0., slippage_rate=.0005,
                                   max_stale_sessions=2, model="next_open"),
                    risk=dict(max_total_weight=1., max_symbol_weight=1., group_caps={}, stress_scenarios=[],
                              pause_action="block_new", recovery_mode="never"),
                    metrics=dict(annual_sessions=252, risk_free_rate=0., cash_rate=0., definition_version="v1"))
    spec = dict(name="tiny-policy", backtest=backtest, lookback=5, episode_sessions=20,
                total_timesteps=16, n_steps=8, batch_size=8, n_epochs=1,
                eval_every_timesteps=8, hidden_size=16,
                train_start=str(dates[10].date()), train_end=str(dates[39].date()),
                validation_start=str(dates[40].date()), validation_end=str(dates[59].date()),
                test_start=str(dates[60].date()), test_end=str(dates[89].date()))
    return spec, dates


