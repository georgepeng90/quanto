from dataclasses import replace
import math

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from quanto.backtest import engine
from quanto.strategies.base import Hold
from tests.fixtures.engine_cases import bundle_case, spec_case, ScriptedStrategy, target


def daily_case(start="2024-01-01", days=45, symbols=("A", "B")):
    bundle = bundle_case(symbols=symbols)
    dates = pd.bdate_range(start, periods=days)
    calendar = pd.bdate_range(dates[0], periods=days + 6)
    prices = pd.DataFrame([dict(date=day, symbol=symbol, open=10., high=10., low=10.,
        close=10., volume=10000., amount=100000.) for day in dates for symbol in symbols])
    return replace(bundle, manifest=bundle.manifest.model_copy(update=dict(calendar_start=calendar[0].date(),
        calendar_end=calendar[-1].date(), data_start=dates[0].date(), data_end=dates[-1].date())),
        prices=prices, benchmark=pd.DataFrame(dict(date=dates, close=10., turnover=100.)),
        calendar=pd.DataFrame(dict(date=calendar)),
        instruments=bundle.instruments.assign(listing_date=dates[0]),
        status=prices[["date", "symbol"]].assign(status="trading", can_buy=True, can_sell=True),
        eligibility=prices[["date", "symbol"]].assign(eligible=True))


def env_for(bundle, **kwargs):
    from quanto.investing.environment import DailyETFEnv
    return DailyETFEnv(bundle, kwargs.pop("spec", spec_case(bundle)), lookback=kwargs.pop("lookback", 2), **kwargs)


def complete(env, action):
    observation, info = env.reset(seed=11)
    rewards = []
    while True:
        observation, reward, terminal, truncated, info = env.step(action)
        rewards.append(reward)
        if terminal or truncated:
            return env.result(), sum(rewards), info


def test_session_runner_preserves_all_backtest_tables():
    assert hasattr(engine, "SessionRunner"), "Interactive daily accounting runner is required"
    bundle = bundle_case()
    spec = spec_case(bundle, execution={"minimum_commission": 1})
    expected = engine.run_backtest(bundle, ScriptedStrategy({1: target(A=.8), 3: target()}), spec, lambda _: None, lambda: False)
    runner = engine.SessionRunner(bundle, spec, lambda: False)
    for day in range(1, 6):
        context = runner.advance_to_close()
        assert context.asof.day == day
        runner.submit_close_intent({1: target(A=.8), 3: target()}.get(day, Hold("保持")))
    actual = runner.result()
    for name in ("equity", "orders", "fills", "positions", "signals", "risk_events", "account_events"):
        assert_frame_equal(getattr(actual, name), getattr(expected, name))


def test_environment_uses_next_open_and_keeps_terminal_positions():
    bundle = daily_case(days=12, symbols=("A",))
    bundle.prices.loc[bundle.prices.date >= "2024-01-08", ["open", "high", "low", "close"]] = 12
    result, rewards, _ = complete(env_for(bundle), np.array([1, 4]))
    assert result.fills.iloc[0].trade_date == pd.Timestamp("2024-01-08")
    assert result.fills.iloc[0].price == 12
    assert result.orders.iloc[0].quantity == 100
    assert result.positions.iloc[-1].shares == 83
    assert rewards == pytest.approx(math.log(result.equity.iloc[-1].equity / 1000))


def test_hold_keeps_shares_and_differs_from_a_rebalance_target():
    bundle = daily_case(days=16, symbols=("A",))
    bundle.prices.loc[bundle.prices.date >= "2024-01-09", ["open", "high", "low", "close"]] = 20
    env = env_for(bundle)
    env.reset()
    env.step(np.array([1, 2]))
    _, _, terminal, _, _ = env.step(np.array([0, 0]))
    while not terminal:
        _, _, terminal, _, _ = env.step(np.array([0, 0]))
    result = env.result()
    assert len(result.fills) == 1
    assert result.positions.iloc[-1].shares == 50


def test_explicit_cash_action_liquidates_while_hold_retains_shares():
    bundle = daily_case(days=16, symbols=("A",))
    hold = env_for(bundle, action_version="hold-target-cash-quartiles-v2")
    cash = env_for(bundle, action_version="hold-target-cash-quartiles-v2")
    for env, second_action in ((hold, [0, 4]), (cash, [2, 4])):
        env.reset()
        env.step(np.array([1, 4]))
        env.step(np.array(second_action))
    assert hold.result().fills.side.tolist() == ["buy"]
    assert cash.result().fills.side.tolist() == ["buy", "sell"]
    assert hold.result().positions.iloc[-1].shares == 100
    assert cash.result().positions.iloc[-1].shares == 0


def test_explicit_cash_action_respects_next_open_sell_block():
    bundle = daily_case(days=16, symbols=("A",))
    bundle.status.loc[bundle.status.date == pd.Timestamp("2024-01-15"), "can_sell"] = False
    env = env_for(bundle, action_version="hold-target-cash-quartiles-v2")
    env.reset()
    env.step(np.array([1, 4]))
    env.step(np.array([2, 4]))
    assert env.result().fills.side.tolist() == ["buy"]
    assert env.result().positions.iloc[-1].shares == 100
    assert env.result().orders.iloc[-1].reason == "cannot_sell"


def test_future_prices_do_not_change_current_observation_or_order_size():
    bundle = daily_case(days=12, symbols=("A",))
    changed = replace(bundle, prices=bundle.prices.copy())
    changed.prices.loc[changed.prices.date > "2024-01-05", ["open", "high", "low", "close"]] = 999
    left, right = env_for(bundle), env_for(changed)
    np.testing.assert_array_equal(left.reset()[0], right.reset()[0])
    left.step(np.array([1, 4]))
    right.step(np.array([1, 4]))
    assert left.result().orders.iloc[0].quantity == right.result().orders.iloc[0].quantity == 100


@pytest.mark.parametrize("lookback", [3, 20, 60, 61, 120])
def test_fixed_horizon_features_use_available_history_independent_of_lookback(lookback):
    bundle = daily_case(days=140, symbols=("A",))
    index = np.arange(140)
    closes = 10 * np.exp(.01 * index)
    for column in ("open", "high", "low", "close"):
        bundle.prices[column] = closes
    bundle.prices["amount"] = 1000 * (index + 1)
    spec = spec_case(bundle, start=bundle.calendar.date.iloc[120].date())
    observation, info = env_for(bundle, spec=spec, lookback=lookback).reset()
    assert info["asof"] == bundle.calendar.date.iloc[124].date().isoformat()
    assert observation[:4] == pytest.approx([.05, .20, .60, .01 * (lookback - 1)])
    assert observation[6] == pytest.approx(math.log(125 / 115.5))
    assert observation[9] == 1.


def test_short_lookback_keeps_configured_drawdown_but_uses_twenty_daily_returns():
    bundle = daily_case(days=100, symbols=("A",))
    bundle.prices.loc[60, ["open", "high", "low", "close"]] = 20.
    bundle.prices.loc[80:, ["open", "high", "low", "close"]] = 9.
    spec = spec_case(bundle, start=bundle.calendar.date.iloc[80].date())
    observation, _ = env_for(bundle, spec=spec, lookback=20).reset()
    # The 20 daily returns contain one 10 -> 9 fall; the older peak is outside lookback.
    assert observation[4] == pytest.approx(abs(math.log(.9)) * math.sqrt(19) / 20)
    assert observation[5] == pytest.approx(-.1)


def test_short_actual_history_uses_only_known_bars_for_fixed_features():
    bundle = daily_case(days=12, symbols=("A",))
    for column in ("open", "high", "low", "close"):
        bundle.prices[column] = 10 * np.exp(.01 * np.arange(12))
    observation, _ = env_for(bundle, lookback=3).reset()
    assert observation[:4] == pytest.approx([.04, .04, .04, .02])
    assert observation[9] == 1.


def test_extending_evaluation_end_does_not_change_current_policy_observations():
    bundle = daily_case(days=20)
    left = env_for(bundle, spec=spec_case(bundle, end="2024-01-16"))
    right = env_for(bundle)
    np.testing.assert_array_equal(left.reset()[0], right.reset()[0])
    left_observation, _, left_terminal, _, left_info = left.step(np.array([1, 2, 1]))
    right_observation, _, right_terminal, _, right_info = right.step(np.array([1, 2, 1]))
    assert not left_terminal and not right_terminal
    assert left_info["asof"] == right_info["asof"] == "2024-01-12"
    np.testing.assert_array_equal(left_observation, right_observation)


def test_week_and_month_boundaries_use_full_calendar():
    from quanto.investing.environment import decision_dates
    calendar = pd.DatetimeIndex(["2024-01-25", "2024-01-26", "2024-01-29", "2024-01-30", "2024-01-31", "2024-02-01", "2024-02-05"])
    assert list(decision_dates(calendar, "2024-01-25", "2024-02-05", "weekly")) == [pd.Timestamp("2024-01-26"), pd.Timestamp("2024-02-01")]
    assert list(decision_dates(calendar, "2024-01-25", "2024-02-05", "monthly")) == [pd.Timestamp("2024-01-31")]
    assert decision_dates(calendar, "2024-01-25", "2024-01-30", "monthly").empty


def test_first_reward_accounts_for_cash_interest_before_first_decision():
    bundle = daily_case(days=12, symbols=("A",))
    spec = spec_case(bundle, metrics={"cash_rate": 2.52})
    result, reward, _ = complete(env_for(bundle, spec=spec), np.array([0, 0]))
    assert len(result.equity) == 12
    assert result.equity.iloc[-1].cash == pytest.approx(1000 * 1.01**12)
    assert reward == pytest.approx(12 * math.log(1.01))


def test_elapsed_sessions_match_the_reward_interval_including_initial_wait():
    env = env_for(daily_case(days=12, symbols=("A",)))
    env.reset()
    _, _, _, _, first = env.step(np.array([0, 0]))
    _, _, _, _, last = env.step(np.array([0, 0]))
    assert first["elapsed_sessions"] == 10
    assert last["elapsed_sessions"] == 2


def test_risk_runs_between_weekly_decisions():
    bundle = daily_case(days=12, symbols=("A",))
    bundle.prices.loc[bundle.prices.date >= "2024-01-09", ["open", "high", "low", "close"]] = 5
    spec = spec_case(bundle, risk={"drawdown_pause": .2, "pause_action": "liquidate"})
    result, _, _ = complete(env_for(bundle, spec=spec), np.array([1, 4]))
    assert result.fills.trade_date.tolist() == [pd.Timestamp("2024-01-08"), pd.Timestamp("2024-01-10")]
    assert result.fills.side.tolist() == ["buy", "sell"]


def test_short_history_cannot_buy_even_when_dataset_eligibility_is_true():
    bundle = daily_case(days=12, symbols=("A", "B"))
    bundle = replace(bundle, prices=bundle.prices.loc[(bundle.prices.symbol == "A") | (bundle.prices.date >= "2024-01-05")].copy())
    env = env_for(bundle, lookback=4)
    env.reset()
    env.step(np.array([1, 0, 4]))
    assert env.result().fills.empty


def test_target_weights_are_normalized_without_leverage():
    bundle = daily_case(days=12)
    result, _, _ = complete(env_for(bundle), np.array([1, 4, 4]))
    assert result.fills.iloc[:2].quantity.tolist() == [50, 50]
    assert (result.equity.cash >= 0).all()


def test_training_episodes_are_seeded_bounded_and_reset_account():
    bundle = daily_case(days=65)
    left, right = env_for(bundle, training=True, episode_sessions=15), env_for(bundle, training=True, episode_sessions=15)
    _, info = left.reset(seed=17)
    _, other = right.reset(seed=17)
    assert info["episode_start"] == other["episode_start"]
    first, _, _ = complete(left, np.array([1, 4, 0]))
    assert len(first.equity) == 15
    assert first.equity.date.min().date() >= bundle.manifest.data_start
    assert first.equity.date.max().date() <= bundle.manifest.data_end
    left.reset(seed=17)
    assert left.runner.ledger.snapshot(left.context.asof).shares == {}


def test_environment_cancellation_and_collect_false_keep_accounting():
    bundle = daily_case(days=12, symbols=("A",))
    cancelled = [False]
    env = env_for(bundle, cancelled=lambda: cancelled[0], collect=False)
    _, info = env.reset()
    cancelled[0] = True
    with pytest.raises(engine.BacktestCancelled):
        env.step(np.array([0, 0]))
    cancelled[0] = False
    collected, rewards, info = complete(env_for(bundle), np.array([1, 4]))
    env.reset()
    done = False
    other_rewards = 0
    while not done:
        _, reward, done, _, other = env.step(np.array([1, 4]))
        other_rewards += reward
    assert other["equity"] == info["equity"]
    assert other_rewards == pytest.approx(rewards)
    assert env.result().equity.empty


def test_official_gymnasium_environment_contract():
    from gymnasium.utils.env_checker import check_env
    check_env(env_for(daily_case(days=20)), skip_render_check=True)
    from stable_baselines3.common.env_checker import check_env as check_sb3_env
    check_sb3_env(env_for(daily_case(days=20)))


def test_environment_account_matches_scripted_weekly_backtest_with_costs():
    bundle = daily_case(days=12)
    bundle.prices.loc[(bundle.prices.symbol == "A") & (bundle.prices.date >= "2024-01-09"), ["open", "high", "low", "close"]] = 12
    spec = spec_case(bundle, execution={"minimum_commission": 1, "slippage_rate": .001})
    expected = engine.run_backtest(bundle, ScriptedStrategy({5: target(A=.5, B=.25), 12: target(A=.5, B=.25)}),
        spec, lambda _: None, lambda: False)
    actual, reward, _ = complete(env_for(bundle, spec=spec), np.array([1, 2, 1]))
    for name in ("equity", "orders", "fills", "positions", "account_events", "risk_events"):
        assert_frame_equal(getattr(actual, name), getattr(expected, name))
    assert reward == pytest.approx(math.log(actual.equity.iloc[-1].equity / spec.initial_cash))


def test_exact_baseline_weights_support_thirds_and_cash_exit():
    bundle = daily_case(days=12, symbols=("A", "B", "C"))
    env = env_for(bundle)
    env.reset()
    env.step_weights({"A": 1 / 3, "B": 1 / 3, "C": 1 / 3})
    _, _, terminated, _, _ = env.step_weights({})
    assert terminated
    result = env.result()
    assert result.fills.quantity.tolist() == [33, 33, 33, 33, 33, 33]
    assert result.positions.iloc[-3:].shares.tolist() == [0, 0, 0]
    assert result.equity.iloc[-1].cash == 1000


@pytest.mark.parametrize("mode", ["raw_cash", "adjusted_research"])
def test_company_action_cashflows_use_existing_price_mode(mode):
    bundle = daily_case(days=12, symbols=("A",))
    bundle.prices.loc[bundle.prices.date >= "2024-01-09", ["open", "high", "low", "close"]] = 9
    events = pd.DataFrame([dict(symbol="A", event_type="dividend", record_date=pd.Timestamp("2024-01-08"),
        ex_date=pd.Timestamp("2024-01-09"), pay_date=pd.Timestamp("2024-01-10"), cash_per_share=1., split_ratio=1.)])
    bundle = replace(bundle, events=events, manifest=bundle.manifest.model_copy(update=dict(price_mode=mode, limitations=["research fixture"])))
    env = env_for(bundle)
    env.reset()
    env.step(np.array([1, 4]))
    env.step(np.array([0, 0]))
    result = env.result()
    assert result.equity.iloc[-1].equity == (1000 if mode == "raw_cash" else 900)
    assert result.equity.iloc[-1].cash == (100 if mode == "raw_cash" else 0)


def test_monthly_decision_and_terminal_date_do_not_invent_a_liquidation():
    bundle = daily_case(days=45, symbols=("A",))
    env = env_for(bundle, frequency="monthly")
    _, info = env.reset()
    assert info["asof"] == "2024-01-31"
    _, _, terminated, _, info = env.step(np.array([1, 4]))
    assert not terminated and info["asof"] == "2024-02-29"
    _, _, terminated, _, info = env.step(np.array([0, 0]))
    assert terminated and info["asof"] == "2024-03-01"
    result = env.result()
    assert result.fills.trade_date.tolist() == [pd.Timestamp("2024-02-01")]
    assert result.positions.iloc[-1].shares == 100
    with pytest.raises(RuntimeError):
        env.step(np.array([1, 0]))


@pytest.mark.parametrize("suspended", [False, True])
def test_missing_held_bar_requires_confirmed_suspension(suspended):
    bundle = daily_case(days=12, symbols=("A",))
    bundle = replace(bundle, prices=bundle.prices.loc[bundle.prices.date != "2024-01-09"].copy())
    if suspended:
        bundle.status.loc[bundle.status.date == "2024-01-09", ["status", "can_buy", "can_sell"]] = ["suspended", False, False]
        result, _, _ = complete(env_for(bundle), np.array([1, 4]))
        assert result.equity.loc[result.equity.date == "2024-01-09", "stale_value"].iloc[0] == 1000
    else:
        env = env_for(bundle)
        env.reset()
        with pytest.raises(ValueError, match="MISSING_PRICE"):
            env.step(np.array([1, 4]))


def test_future_split_does_not_adjust_current_price_features():
    bundle = daily_case(days=12, symbols=("A",))
    events = pd.DataFrame([dict(symbol="A", event_type="split", record_date=pd.NaT,
        ex_date=pd.Timestamp("2024-01-08"), pay_date=pd.NaT, cash_per_share=0., split_ratio=2.)])
    changed = replace(bundle, events=events, prices=bundle.prices.copy())
    changed.prices.loc[changed.prices.date >= "2024-01-08", ["open", "high", "low", "close"]] = 5
    left, right = env_for(bundle), env_for(changed)
    np.testing.assert_array_equal(left.reset()[0], right.reset()[0])
    _, _, _, _, info = right.step(np.array([1, 4]))
    assert info["equity"] == 1000
    assert right.result().fills.iloc[0].quantity == 200


STATE_REWARDS = ["net-log-return-with-state-v1", "differential-sharpe-v1"]


@pytest.mark.parametrize("reward_version", STATE_REWARDS)
def test_state_reward_prefix_updates_moments_without_rewarding_first_action(reward_version):
    bundle = daily_case(days=12, symbols=("A",))
    spec = spec_case(bundle, metrics={"cash_rate": 2.52})
    env = env_for(bundle, spec=spec, reward_version=reward_version)
    observation, prefix = env.reset()
    assert observation.shape == (18,)
    assert prefix["prefix_net_log_return"] == pytest.approx(5 * math.log(1.01))
    assert prefix["reward_daily_count"] == 5
    assert observation[-2:] == pytest.approx([.000490099501 / .010490099501, .5])
    _, reward, _, _, first = env.step(np.array([0, 0]))
    assert first["elapsed_sessions"] == first["reward_daily_count"] == 5
    assert first["step_net_log_return"] == pytest.approx(5 * math.log(1.01))
    if reward_version == STATE_REWARDS[0]:
        assert reward == pytest.approx(first["step_net_log_return"])
    _, last_reward, done, _, last = env.step(np.array([0, 0]))
    assert done and last["reward_daily_count"] == last["elapsed_sessions"] == 2
    assert prefix["prefix_net_log_return"] + first["step_net_log_return"] + last["step_net_log_return"] == pytest.approx(12 * math.log(1.01))
    assert env.reward_diagnostics["reward_daily_count"] == 12
    assert env.reward_diagnostics["reward_sum"] == pytest.approx(reward + last_reward)


def test_dsr_aggregates_daily_account_returns_with_costs_and_matches_control_state():
    bundle = daily_case(days=12, symbols=("A",))
    for day, close in [("2024-01-08", 11.), ("2024-01-09", 9.), ("2024-01-10", 12.)]:
        bundle.prices.loc[bundle.prices.date == day, ["open", "high", "low", "close"]] = close
    spec = spec_case(bundle, execution={"minimum_commission": 1., "slippage_rate": .001})
    control, dsr = [env_for(bundle, spec=spec, reward_version=version) for version in STATE_REWARDS]
    np.testing.assert_array_equal(control.reset()[0], dsr.reset()[0])
    observation, reward, _, _, info = dsr.step(np.array([1, 4]))
    other, log_reward, _, _, control_info = control.step(np.array([1, 4]))
    np.testing.assert_array_equal(observation, other)
    assert log_reward == info["step_net_log_return"] == control_info["step_net_log_return"]
    # 90 shares at 11.011 plus one unit commission leaves 8.01 cash.
    # The boundary close is not exported until its intent is submitted.
    equities = [1000.] * 5 + [998.01, 818.01, 1088.01, 908.01, 908.01]
    mean, second = 0., .0001
    expected, max_abs = 0., 0.
    for index, equity in enumerate(equities):
        daily = equity / (equities[index - 1] if index else spec.initial_cash) - 1.
        delta_mean, delta_second = daily - mean, daily * daily - second
        daily_dsr = (second * delta_mean - .5 * mean * delta_second) / max(second - mean * mean, 1e-8)**1.5
        mean += .01 * delta_mean
        second += .01 * delta_second
        if index >= 5:
            expected += daily_dsr
            max_abs = max(max_abs, abs(daily_dsr))
    assert reward == pytest.approx(expected)
    assert info["reward_dsr_max_abs"] == pytest.approx(max_abs)
    assert info["step_net_log_return"] == pytest.approx(math.log(equities[-1] / equities[4]))
    assert_frame_equal(control.result().equity, dsr.result().equity)


@pytest.mark.parametrize("reward_version", STATE_REWARDS)
def test_state_reward_collect_false_and_reset_keep_session_diagnostics(reward_version):
    bundle = daily_case(days=12, symbols=("A",))
    bundle.prices.loc[bundle.prices.date >= "2024-01-09", ["open", "high", "low", "close"]] = 12.
    collected, light = [env_for(bundle, reward_version=reward_version, collect=collect) for collect in (True, False)]
    first_observation, _ = light.reset(seed=5)
    collected.reset(seed=5)
    done = False
    while not done:
        observation, reward, done, _, info = light.step(np.array([1, 4]))
        other, other_reward, other_done, _, other_info = collected.step(np.array([1, 4]))
        np.testing.assert_array_equal(observation, other)
        assert reward == other_reward and done == other_done and info == other_info
    assert light.result().equity.empty
    total = light.reward_diagnostics.copy()
    observation, prefix = light.reset(seed=5)
    np.testing.assert_array_equal(observation, first_observation)
    assert light.reward_diagnostics["reward_sum"] == total["reward_sum"]
    assert light.reward_diagnostics["reward_daily_count"] == total["reward_daily_count"] + prefix["reward_daily_count"]


@pytest.mark.parametrize("reward_version", STATE_REWARDS)
def test_state_observation_and_statistics_ignore_future_prices(reward_version):
    bundle = daily_case(days=12, symbols=("A",))
    changed = replace(bundle, prices=bundle.prices.copy())
    changed.prices.loc[changed.prices.date > "2024-01-05", ["open", "high", "low", "close"]] = 999.
    left, right = [env_for(data, reward_version=reward_version) for data in (bundle, changed)]
    np.testing.assert_array_equal(left.reset()[0], right.reset()[0])
    assert left.reward_diagnostics == right.reward_diagnostics


@pytest.mark.parametrize("reward_version", STATE_REWARDS)
def test_state_monthly_reward_covers_terminal_short_interval(reward_version):
    bundle = daily_case(days=45, symbols=("A",))
    spec = spec_case(bundle, metrics={"cash_rate": 2.52})
    env = env_for(bundle, spec=spec, frequency="monthly", reward_version=reward_version)
    _, prefix = env.reset()
    _, _, done, _, first = env.step(np.array([0, 0]))
    assert not done and prefix["reward_daily_count"] == 23 and first["reward_daily_count"] == 21
    _, _, done, _, last = env.step(np.array([0, 0]))
    assert done and last["reward_daily_count"] == last["elapsed_sessions"] == 1
    assert prefix["prefix_net_log_return"] + first["step_net_log_return"] + last["step_net_log_return"] == pytest.approx(45 * math.log(1.01))


def test_reward_identity_preserves_legacy_and_shares_new_state_shape():
    from quanto.investing.environment import feature_version, environment_version
    assert feature_version(61) == "daily-etf-v2"
    assert feature_version(3) == "daily-etf-v3"
    assert environment_version() == "daily-session-v1"
    for version in STATE_REWARDS:
        assert feature_version(61, version) == "daily-etf-v2-dsr-state-v1"
        assert feature_version(3, version) == "daily-etf-v3-dsr-state-v1"
        assert environment_version(version) == "daily-session-reward-state-v2"


def test_environment_rejects_unknown_reward_version():
    with pytest.raises(ValueError, match="reward version"):
        env_for(daily_case(days=12), reward_version="unknown")


@pytest.mark.parametrize("equity", [0., -1., float("nan"), float("inf")])
@pytest.mark.parametrize("reward_version", STATE_REWARDS)
def test_state_rewards_fail_on_invalid_daily_equity_at_prefix_close(monkeypatch, equity, reward_version):
    advance = engine.SessionRunner.advance_to_close

    def invalid_close(runner):
        context = advance(runner)
        if context.asof == pd.Timestamp("2024-01-03"):
            return replace(context, portfolio=replace(context.portfolio, equity=equity))
        return context

    monkeypatch.setattr(engine.SessionRunner, "advance_to_close", invalid_close)
    env = env_for(daily_case(days=12), reward_version=reward_version, collect=False)
    with pytest.raises(ValueError, match="Positive finite daily equity"):
        env.reset()


@pytest.mark.parametrize("reward_version", STATE_REWARDS)
def test_state_rewards_include_dividend_receivables_without_double_counting(reward_version):
    bundle = daily_case(days=12, symbols=("A",))
    bundle.prices.loc[bundle.prices.date >= "2024-01-09", ["open", "high", "low", "close"]] = 9.
    events = pd.DataFrame([dict(symbol="A", event_type="dividend", record_date=pd.Timestamp("2024-01-08"),
        ex_date=pd.Timestamp("2024-01-09"), pay_date=pd.Timestamp("2024-01-10"), cash_per_share=1., split_ratio=1.)])
    env = env_for(replace(bundle, events=events), reward_version=reward_version)
    env.reset()
    _, reward, _, _, info = env.step(np.array([1, 4]))
    assert reward == pytest.approx(0.)
    assert info["step_net_log_return"] == pytest.approx(0.)
    assert info["equity"] == 1000.


@pytest.mark.parametrize("reward_version", STATE_REWARDS)
def test_state_rewards_satisfy_gymnasium_contract(reward_version):
    from gymnasium.utils.env_checker import check_env
    check_env(env_for(daily_case(days=20), reward_version=reward_version), skip_render_check=True)
