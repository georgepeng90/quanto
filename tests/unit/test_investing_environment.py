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


def test_future_prices_do_not_change_current_observation_or_order_size():
    bundle = daily_case(days=12, symbols=("A",))
    changed = replace(bundle, prices=bundle.prices.copy())
    changed.prices.loc[changed.prices.date > "2024-01-05", ["open", "high", "low", "close"]] = 999
    left, right = env_for(bundle), env_for(changed)
    np.testing.assert_array_equal(left.reset()[0], right.reset()[0])
    left.step(np.array([1, 4]))
    right.step(np.array([1, 4]))
    assert left.result().orders.iloc[0].quantity == right.result().orders.iloc[0].quantity == 100


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
