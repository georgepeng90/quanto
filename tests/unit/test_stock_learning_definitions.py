from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.stock_trading import STOCK_TRADING_DEFINITION
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.definitions import create_environment, etf_learning_definitions
from quanto.investing.stock_definitions import STOCK_MARKET_FEATURES, STOCK_PPO_DEFINITION
from tests.fixtures.stock_training_cases import stock_case, stock_binding


def test_registered_stock_definitions_and_unchanged_etf_hashes():
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.investing.legacy_evidence import audited_etf_semantics
    from quanto.workbench.definitions import DefinitionRegistry
    registry = DefinitionRegistry()
    for definition in (STOCK_MARKET_FEATURES, STOCK_PPO_DEFINITION, STOCK_TRADING_DEFINITION):
        assert registry.get(definition.ref) is definition
        assert definition.supported_instrument_types == ("stock",)
    for definition in etf_learning_definitions():
        assert audited_etf_semantics(definition, ETF_TRADING_DEFINITION)


def test_stock_observation_is_causal_and_cross_asset_definitions_are_rejected(tmp_path):
    raw, dates = stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    config = PolicyTrainingSpec.model_validate(raw)
    binding = stock_binding(bundle)
    def env(data, definition=STOCK_PPO_DEFINITION):
        return create_environment(definition, data, config, None, backtest=config.backtest, training=False,
                                  asset_binding=binding, trading_definition=STOCK_TRADING_DEFINITION, collect=True)
    before = env(bundle)
    first = before.reset(seed=42)[0]
    changed = replace(bundle, prices=bundle.prices.copy(), status=bundle.status.copy())
    changed.prices.loc[changed.prices.date >= dates[30], "close"] *= 2
    changed.status.loc[changed.status.date >= dates[30], "can_buy"] = False
    changed = replace(changed, events=pd.DataFrame([dict(symbol="000001", event_type="split", record_date=pd.NaT,
        ex_date=dates[40], pay_date=pd.NaT, cash_per_share=None, split_ratio=2.)]))
    after = env(changed)
    np.testing.assert_array_equal(first, after.reset(seed=42)[0])
    assert before.action_space.nvec.tolist() == [3, 5]
    with pytest.raises(ValueError, match="DEFINITION_UNAVAILABLE"):
        env(bundle, next(d for d in etf_learning_definitions() if d.action_version == config.action_version and d.reward_version == config.reward_version))
    before.close()
    after.close()


def test_stock_step_weights_uses_the_same_daily_account_as_fixed_targets(tmp_path):
    from quanto.backtest.engine import run_backtest
    from quanto.strategies.base import Hold, TargetPortfolio
    raw, _ = stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    config = PolicyTrainingSpec.model_validate(raw)
    binding = stock_binding(bundle)
    env = create_environment(STOCK_PPO_DEFINITION, bundle, config, None, backtest=config.backtest, training=False,
                             asset_binding=binding, trading_definition=STOCK_TRADING_DEFINITION, collect=True)
    env.reset(seed=42)
    actions = {}
    done = False
    while not done:
        actions[env.context.asof] = TargetPortfolio({"000001": .5}, {}, {})
        _, _, done, _, _ = env.step_weights({"000001": .5})
    class Fixed:
        warmup_bars = 0

        def should_evaluate(self, context):
            return True

        def on_close(self, context):
            return actions.get(context.asof, Hold("between decisions"))
    fixed = run_backtest(bundle, Fixed(), config.backtest, lambda event: None, lambda: False,
                         trading_definition=STOCK_TRADING_DEFINITION, asset_binding=binding)
    pd.testing.assert_frame_equal(env.result().fills, fixed.fills)
    pd.testing.assert_frame_equal(env.result().equity, fixed.equity)
    env.close()
