from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.stock_trading import STOCK_TRADING_DEFINITION
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.definitions import create_environment, etf_learning_definitions
from quanto.investing.stock_definitions import (STOCK_MARKET_FEATURES, STOCK_PPO_DEFINITION,
    STOCK_PPO_DSR_DEFINITION, STOCK_PPO_STATE_LOG_DEFINITION)
from tests.fixtures.stock_training_cases import stock_case, stock_binding


def test_registered_stock_definitions_and_unchanged_etf_hashes():
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.investing.legacy_evidence import audited_etf_semantics
    from quanto.workbench.definitions import DefinitionRegistry
    registry = DefinitionRegistry()
    stock_learning = (STOCK_PPO_DEFINITION, STOCK_PPO_STATE_LOG_DEFINITION, STOCK_PPO_DSR_DEFINITION)
    for definition in (STOCK_MARKET_FEATURES, *stock_learning, STOCK_TRADING_DEFINITION):
        assert registry.get(definition.ref) is definition
        assert definition.supported_instrument_types == ("stock",)
    assert [definition.ref.id for definition in stock_learning] == [
        "stock-ppo-cash-log", "stock-ppo-cash-state-log", "stock-ppo-cash-dsr"]
    assert [definition.reward_version for definition in stock_learning] == [
        "net-log-return-v1", "net-log-return-with-state-v1", "differential-sharpe-v1"]
    assert len({definition.definition_hash for definition in stock_learning}) == 3
    assert all(definition.action_version == STOCK_PPO_DEFINITION.action_version for definition in stock_learning)
    assert all(definition.feature_definition.definition_hash == STOCK_MARKET_FEATURES.definition_hash
               for definition in stock_learning)
    for definition in stock_learning:
        registry.snapshot(definition, {"lookback": 5, "frequency": "weekly",
            "action_version": definition.action_version, "reward_version": definition.reward_version})
    assert STOCK_PPO_DEFINITION.reward_version == "net-log-return-v1"
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


def test_stock_state_reward_methods_keep_identical_observation_and_cash_action_shapes(tmp_path):
    definitions = (STOCK_PPO_STATE_LOG_DEFINITION, STOCK_PPO_DSR_DEFINITION)
    raw, _ = stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    binding = stock_binding(bundle)
    environments = []
    for definition in definitions:
        config = PolicyTrainingSpec.model_validate({**raw, "reward_version": definition.reward_version})
        env = create_environment(definition, bundle, config, None, backtest=config.backtest, training=False,
            asset_binding=binding, trading_definition=STOCK_TRADING_DEFINITION, collect=True)
        observation, _ = env.reset(seed=42)
        environments.append((env, observation))
        with pytest.raises(ValueError, match="parameter"):
            definition.validate_config(config.model_copy(update={"reward_version": STOCK_PPO_DEFINITION.reward_version}))
    base_config = PolicyTrainingSpec.model_validate(raw)
    base_env = create_environment(STOCK_PPO_DEFINITION, bundle, base_config, None, backtest=base_config.backtest,
        training=False, asset_binding=binding, trading_definition=STOCK_TRADING_DEFINITION, collect=False)
    base_observation, _ = base_env.reset(seed=42)
    for env, observation in environments:
        assert observation.shape == (base_observation.shape[0] + 2,)
        assert env.action_space.nvec.tolist() == [3, 5]
        np.testing.assert_array_equal(observation[:-2], base_observation)
    base_env.close()

    state_log_rewards, dsr_rewards = [], []
    state_log_observations, dsr_observations = [], []
    done = False
    while not done:
        state_log_step = environments[0][0].step_weights({"000001": .5})
        dsr_step = environments[1][0].step_weights({"000001": .5})
        state_log_observations.append(state_log_step[0])
        dsr_observations.append(dsr_step[0])
        state_log_rewards.append(state_log_step[1])
        dsr_rewards.append(dsr_step[1])
        assert state_log_step[2] == dsr_step[2]
        done = state_log_step[2]
    assert any(not np.array_equal(observation[-2:], environments[0][1][-2:])
               for observation in state_log_observations)
    for state_observation, dsr_observation in zip(state_log_observations, dsr_observations):
        np.testing.assert_array_equal(state_observation, dsr_observation)
    assert not np.allclose(state_log_rewards, dsr_rewards)
    state_log_result, dsr_result = environments[0][0].result(), environments[1][0].result()
    for field in ("equity", "orders", "fills", "positions", "account_events"):
        pd.testing.assert_frame_equal(getattr(state_log_result, field), getattr(dsr_result, field))
    for env, _ in environments:
        env.close()


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
