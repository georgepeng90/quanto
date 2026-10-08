"""ETF learning definitions preserve the frozen pre-framework account behavior."""
from __future__ import annotations

from datetime import date
from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest

from quanto.backtest.dataset_identity import standard_table_semantics
from quanto.config import BacktestSpec
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding
from quanto.investing.contracts import PolicyTrainingSpec
from tests.fixtures.etf_framework_baseline import REFERENCE_PATH, case_definitions, reference_bundle, _table


def _binding(bundle):
    assets = tuple(AssetMapping(instrument_id=InstrumentId(exchange=exchange, instrument_type="etf", code=symbol),
                                local_symbol=symbol, evidence_ref="fixture:explicit-etf")
                   for exchange, symbol in (("SSE", "510300"), ("SZSE", "159915")))
    return AssetBinding(dataset_version_id="fixture", dataset_content_hash="a" * 64, assets=assets,
                        benchmark=BenchmarkBinding(custom_series_id="fixture-index", return_basis="price_index",
                                                   evidence_ref="fixture:explicit-etf"),
                        table_semantics=standard_table_semantics(bundle, "fixture:explicit-etf"))


def _config(case, backtest):
    return PolicyTrainingSpec.model_validate(dict(name="frozen ETF behavior", backtest=backtest.model_dump(mode="json"),
        frequency=case["frequency"], lookback=case["lookback"], action_version=case["action"],
        reward_version=case["reward"], train_start=date(2024, 1, 1), train_end=date(2024, 1, 12),
        validation_start=date(2024, 1, 15), validation_end=date(2024, 1, 31)))


def test_only_the_six_supported_etf_action_reward_combinations_are_registered():
    from quanto.investing.definitions import etf_learning_definitions

    definitions = etf_learning_definitions()
    assert len(definitions) == 6
    assert {(item.action_version, item.reward_version) for item in definitions} == {
        (action, reward) for action in ("hold-target-quartiles-v1", "hold-target-cash-quartiles-v2")
        for reward in ("net-log-return-v1", "net-log-return-with-state-v1", "differential-sharpe-v1")}
    assert all(item.feature_definition.supported_instrument_types == ("etf",) for item in definitions)
    assert all(item.supported_instrument_types == ("etf",) for item in definitions)


def test_learning_data_requirements_match_registered_daily_field_units():
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.framework import FrozenBasketRevision, ResearchInterval
    from quanto.investing.definitions import etf_learning_definitions

    bundle = reference_bundle()
    binding = _binding(bundle)
    basket = FrozenBasketRevision(basket_id="fixture", revision=1,
        members=tuple(asset.instrument_id for asset in binding.assets), selection_note="fixture",
        created_at="2024-01-01T00:00:00Z")
    definition = etf_learning_definitions()[0]
    params = dict(lookback=61, frequency="weekly", action_version=definition.action_version,
                  reward_version=definition.reward_version)
    interval = ResearchInterval(start=date(2024, 1, 1), end=date(2024, 1, 31), warmup_start=date(2023, 9, 1))
    requirements = (definition.resolve_requirements(params, basket, interval)
                    + ETF_TRADING_DEFINITION.resolve_requirements({}, basket, interval))
    schemas = {item.table: item.schema_definition for item in binding.table_semantics}
    for requirement in requirements:
        for field in requirement.fields:
            assert schemas[requirement.table][field.name]["unit"] == field.unit
            if (requirement.table == "prices" and field.name in {"open", "close", "high", "low", "amount"}
                    or requirement.table == "events" and field.name == "cash_per_share"):
                assert field.currency == "CNY"


@pytest.mark.parametrize("case", case_definitions(), ids=lambda case: case["id"])
def test_bound_etf_factory_replays_original_observations_rewards_and_ledger(case):
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.investing.definitions import create_environment, etf_learning_definitions

    frozen = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["cases"][case["id"]]
    bundle = reference_bundle("adjusted_research" if "adjusted" in case["dataset"] else "raw_cash",
                              monthly="monthly" in case["dataset"], risk=case["risk"])
    backtest = BacktestSpec.model_validate(frozen["backtest_spec"])
    config = _config(case, backtest)
    definition = next(item for item in etf_learning_definitions()
                      if (item.action_version, item.reward_version) == (case["action"], case["reward"]))
    env = create_environment(definition, bundle, config, None, backtest=backtest, training=False,
                             asset_binding=_binding(bundle), trading_definition=ETF_TRADING_DEFINITION, collect=True)
    try:
        observation, _ = env.reset(seed=17)
        np.testing.assert_array_equal(observation, frozen["initial"]["observation"])
        assert len(env.context.features.per_symbol[backtest.symbols[0]]) == 9
        for step in frozen["trace"]:
            observation, reward, terminated, truncated, _ = env.step(np.array(step["action"], dtype=np.int64))
            np.testing.assert_array_equal(observation, step["observation"])
            assert reward == pytest.approx(step["reward"], rel=1e-12, abs=1e-12)
            assert (terminated, truncated) == (step["terminal"], step["truncated"])
        result = env.result()
        for name, expected in frozen["tables"].items():
            assert _table(getattr(result, name)) == expected
    finally:
        env.close()


def test_raw_cash_market_frame_adjusts_only_splits_that_have_occurred():
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.investing.definitions import create_environment, etf_learning_definitions
    from quanto.investing.environment import DailyETFEnv

    case = next(item for item in case_definitions() if item["id"] == "weekly-hold-target-quartiles-v1-net-log-return-v1")
    original = reference_bundle()
    split = pd.DataFrame([dict(symbol="510300", event_type="split", record_date=pd.Timestamp("2024-01-08"),
                               ex_date=pd.Timestamp("2024-01-09"), pay_date=pd.NaT,
                               cash_per_share=None, split_ratio=2.)])
    bundle = replace(original, events=pd.concat([original.events, split], ignore_index=True))
    frozen = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["cases"][case["id"]]
    backtest = BacktestSpec.model_validate(frozen["backtest_spec"])
    config = _config(case, backtest)
    definition = next(item for item in etf_learning_definitions()
                      if (item.action_version, item.reward_version) == (case["action"], case["reward"]))
    bound = create_environment(definition, bundle, config, None, backtest=backtest, training=False,
                               asset_binding=_binding(bundle), trading_definition=ETF_TRADING_DEFINITION)
    direct = DailyETFEnv(bundle, backtest, frequency=case["frequency"], lookback=case["lookback"],
                         action_version=case["action"], reward_version=case["reward"])
    try:
        np.testing.assert_array_equal(bound.reset(seed=17)[0], frozen["initial"]["observation"])
        direct.reset(seed=17)
        action = np.array([1, 4, 4], dtype=np.int64)
        bound_observation = bound.step(action)[0]
        direct_observation = direct.step(action)[0]
        np.testing.assert_array_equal(bound_observation, direct_observation)
        assert bound_observation[0] > .6
        assert any(":events:" in source for source in
                   bound.context.features.per_symbol["510300"]["return_5"].source_refs)
    finally:
        bound.close()
        direct.close()


def test_injected_learning_hooks_control_schedule_observation_action_reward_and_identity():
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.investing.definitions import create_environment, etf_learning_definitions
    from quanto.investing.environment import decision_dates
    from quanto.strategies.base import Hold

    case = next(item for item in case_definitions() if item["id"] == "weekly-hold-target-quartiles-v1-net-log-return-v1")
    bundle = reference_bundle(monthly=True)
    frozen = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["cases"]["monthly-legacy"]
    backtest = BacktestSpec.model_validate(frozen["backtest_spec"])
    config = _config(case, backtest)
    preset = next(item for item in etf_learning_definitions()
                  if (item.action_version, item.reward_version) == (case["action"], case["reward"]))
    injected = replace(preset,
        observation_encoder=lambda env: np.full(env.observation_space.shape, 7., dtype=np.float32),
        action_decoder=lambda env, action: Hold("injected hold"),
        reward_hook=lambda env, interval, equity: 9.,
        schedule_hook=lambda calendar, start, end, frequency: decision_dates(calendar, start, end, "monthly"))
    assert injected.definition_hash != preset.definition_hash
    env = create_environment(injected, bundle, config, None, backtest=backtest, training=False,
                             asset_binding=_binding(bundle), trading_definition=ETF_TRADING_DEFINITION, collect=True)
    try:
        observation, info = env.reset(seed=17)
        assert info["asof"] == "2024-01-31"
        np.testing.assert_array_equal(observation, np.full(observation.shape, 7., dtype=np.float32))
        _, reward, _, _, _ = env.step(np.array([1, 4, 4], dtype=np.int64))
        assert reward == 9.
        assert env.result().fills.empty
    finally:
        env.close()


def test_fixed_and_bound_learning_paths_use_the_same_ledger_for_identical_intents():
    from quanto.backtest.engine import run_backtest
    from quanto.backtest.models import TABLE_COLUMNS
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.investing.definitions import create_environment, etf_learning_definitions
    from quanto.strategies.base import Hold

    case = next(item for item in case_definitions()
                if item["id"] == "weekly-hold-target-cash-quartiles-v2-net-log-return-v1")
    frozen = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["cases"][case["id"]]
    bundle = reference_bundle()
    backtest = BacktestSpec.model_validate(frozen["backtest_spec"])
    config = _config(case, backtest)
    binding = _binding(bundle)
    definition = next(item for item in etf_learning_definitions()
                      if (item.action_version, item.reward_version) == (case["action"], case["reward"]))
    env = create_environment(definition, bundle, config, None, backtest=backtest, training=False,
                             asset_binding=binding, trading_definition=ETF_TRADING_DEFINITION, collect=True)
    intents = {}
    try:
        env.reset(seed=17)
        for step in frozen["trace"]:
            action = np.array(step["action"], dtype=np.int64)
            intents[env.context.asof] = definition.decode_action(env, action)
            env.step(action)
        learning_ledger = env.result()
    finally:
        env.close()

    class ReplayIntents:
        warmup_bars = 0

        def should_evaluate(self, context):
            return True

        def on_close(self, context):
            return intents.get(context.asof, Hold("between policy decisions"))

    fixed_ledger = run_backtest(bundle, ReplayIntents(), backtest, lambda event: None, lambda: False,
                                trading_definition=ETF_TRADING_DEFINITION, asset_binding=binding,
                                feature_definition=definition.feature_definition,
                                feature_params=definition.feature_params(config))
    for name, columns in TABLE_COLUMNS.items():
        assert _table(getattr(fixed_ledger, name)[columns]) == _table(getattr(learning_ledger, name)[columns]), name
