from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.stock_trading import STOCK_TRADING_DEFINITION
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.definitions import create_environment
from quanto.investing.stock_definitions import STOCK_PPO_STATE_LOG_DEFINITION, STOCK_PPO_DSR_DEFINITION
from tests.fixtures.stock_training_cases import stock_case, stock_binding


def make_environment(bundle, config, definition, collect):
    return create_environment(definition, bundle, config, None, backtest=config.backtest,
        training=True, asset_binding=stock_binding(bundle), trading_definition=STOCK_TRADING_DEFINITION,
        collect=collect)


def test_stock_reset_reuses_market_features_but_resets_account(tmp_path):
    raw, _ = stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    definition = STOCK_PPO_STATE_LOG_DEFINITION
    config = PolicyTrainingSpec.model_validate({**raw, "reward_version": definition.reward_version})
    computed = []
    original = definition.feature_definition.compute

    def compute(view, params):
        computed.append(view.asof)
        return original(view, params)

    definition = replace(definition, feature_definition=replace(definition.feature_definition, compute=compute))
    env = make_environment(bundle, config, definition, True)
    first, info = env.reset(seed=42)
    frame = env.context.features
    env.step(np.array([1, 4]))
    assert env.context.portfolio.shares
    ledger = env.runner.ledger
    count = len(computed)
    repeated, repeated_info = env.reset(seed=42)
    np.testing.assert_array_equal(first, repeated)
    assert info == repeated_info
    assert env.context.features is frame
    assert len(computed) == count
    assert env.runner.ledger is not ledger
    assert not env.context.portfolio.shares
    env.close()


@pytest.mark.parametrize("definition", [STOCK_PPO_STATE_LOG_DEFINITION, STOCK_PPO_DSR_DEFINITION])
@pytest.mark.parametrize("collect", [False, True])
def test_stock_cached_features_match_fresh_views_across_episodes(tmp_path, definition, collect):
    raw, _ = stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    config = PolicyTrainingSpec.model_validate({**raw, "reward_version": definition.reward_version})
    cached = make_environment(bundle, config, definition, collect)
    fresh = make_environment(bundle, config, definition, collect)
    actions = [np.array([1, 4]), np.array([0, 0]), np.array([1, 1]), np.array([2, 0])]
    for episode in range(3):
        fresh._session_data = None
        seed = 42 if episode == 0 else None
        actual, info = cached.reset(seed=seed)
        expected, other_info = fresh.reset(seed=seed)
        np.testing.assert_array_equal(actual, expected)
        assert info == other_info
        assert cached.context.features.frame_hash == fresh.context.features.frame_hash
        done, step = False, 0
        while not done:
            actual = cached.step(actions[step % len(actions)])
            expected = fresh.step(actions[step % len(actions)])
            np.testing.assert_array_equal(actual[0], expected[0])
            assert actual[1:] == expected[1:]
            assert cached.context.features.frame_hash == fresh.context.features.frame_hash
            done = actual[2] or actual[3]
            step += 1
        for name in ("equity", "orders", "fills", "positions", "signals", "account_events", "risk_events"):
            pd.testing.assert_frame_equal(getattr(cached.result(), name), getattr(fresh.result(), name), check_exact=True)
        assert cached.reward_diagnostics == fresh.reward_diagnostics
        assert cached.training_sampling_intervals == fresh.training_sampling_intervals
    cached.close()
    fresh.close()
