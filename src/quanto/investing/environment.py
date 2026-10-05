"""Gymnasium adapter for causal daily ETF accounts and weekly/monthly actions."""
from __future__ import annotations

import math
from typing import Callable

import gymnasium as gym
from gymnasium import spaces
import numpy as np
import pandas as pd

from quanto.backtest.dataset_schemas import DatasetBundle
from quanto.backtest.engine import SessionRunner
from quanto.backtest.models import BacktestResult
from quanto.config import BacktestSpec
from quanto.strategies.base import Hold, TargetPortfolio, validate_intent


FEATURE_VERSION = "daily-etf-v2"
ACTION_VERSION = "hold-target-quartiles-v1"
ENVIRONMENT_VERSION = "daily-session-v1"
EXECUTION_VERSION = "next-open-ledger-v1"


def feature_version(lookback: int) -> str:
    # Only shorter windows change observations when fixed horizons retain 61 bars.
    return "daily-etf-v3" if lookback < 61 else FEATURE_VERSION


def decision_dates(calendar, start, end, frequency: str) -> pd.DatetimeIndex:
    """Natural period ends with a following execution session inside the run."""
    if frequency not in {"weekly", "monthly"}:
        raise ValueError("frequency must be weekly or monthly")
    if isinstance(calendar, pd.DataFrame):
        calendar = calendar["date"]
    dates = pd.DatetimeIndex(pd.to_datetime(calendar)).sort_values().unique()
    periods = dates.to_period("W-FRI" if frequency == "weekly" else "M")
    if len(dates) < 2:
        return dates[:0]
    valid = (periods[:-1] != periods[1:]) & (dates[:-1] >= pd.Timestamp(start)) & (dates[1:] <= pd.Timestamp(end))
    return dates[:-1][valid]


class DailyETFEnv(gym.Env):
    """One action spans a natural decision period, with daily account/risk checks.

    Each symbol contributes 12 fixed-scaled historical/account features: log
    returns over 5/20/60/lookback bars, 20-bar daily log-return volatility,
    lookback price drawdown, log amount ratio, current bar range, quote mask,
    entry eligibility, actual weight, and sellable-share fraction. Portfolio
    features are cash, receivables, pause state, and drawdown.
    No fitted normalization statistics or future price filling are used.
    """

    metadata = {"render_modes": []}

    def __init__(self, bundle: DatasetBundle, spec: BacktestSpec, frequency: str = "weekly",
                 lookback: int = 120, episode_sessions: int | None = None, training: bool = False,
                 *, cancelled: Callable[[], bool] = lambda: False, collect: bool = True):
        super().__init__()
        spec.validate_for_dataset(bundle)
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 2:
            raise ValueError("lookback must be an integer of at least 2")
        if episode_sessions is not None and (isinstance(episode_sessions, bool) or not isinstance(episode_sessions, int) or episode_sessions < 2):
            raise ValueError("episode_sessions must be an integer of at least 2")
        self.bundle, self.backtest_spec = bundle, spec
        self.frequency, self.lookback = frequency, lookback
        self.episode_sessions, self.training = episode_sessions, training
        self.cancelled, self.collect = cancelled, collect
        self.calendar = pd.DatetimeIndex(bundle.calendar.date).sort_values().unique()
        self.sessions = self.calendar[(self.calendar >= pd.Timestamp(spec.start)) & (self.calendar <= pd.Timestamp(spec.end))]
        self._decisions = decision_dates(self.calendar, spec.start, spec.end, frequency)
        if self._decisions.empty:
            raise ValueError("No decision date with a following execution session")
        self._episode_starts = np.array([0], dtype=np.int64)
        if training and episode_sessions is not None:
            if episode_sessions > len(self.sessions):
                raise ValueError("episode_sessions exceeds the training interval")
            # Retain only fragments containing a natural decision and execution.
            starts = []
            for index in range(len(self.sessions) - episode_sessions + 1):
                end = self.sessions[index + episode_sessions - 1]
                if ((self._decisions >= self.sessions[index]) & (self._decisions < end)).any():
                    starts.append(index)
            if not starts:
                raise ValueError("No episode fragment contains a decision and execution")
            self._episode_starts = np.asarray(starts, dtype=np.int64)
        self._history = {symbol: frame.sort_values("date").set_index("date")
                         for symbol, frame in bundle.prices.groupby("symbol") if symbol in spec.symbols}
        self.action_space = spaces.MultiDiscrete(np.array([2] + [5] * len(spec.symbols)))
        self.observation_space = spaces.Box(-10., 10., shape=(12 * len(spec.symbols) + 4,), dtype=np.float32)
        self.runner = None
        self.context = None
        self._done = True

    def _symbol_window(self, symbol):
        frame = self._history.get(symbol)
        if frame is None:
            return None
        stop = frame.index.searchsorted(self.context.asof, side="right")
        return frame.iloc[max(0, stop - max(self.lookback, 61)):stop]

    def _entry_symbols(self) -> set[str]:
        eligible = set(self.context.eligible_symbols)
        return {symbol for symbol in eligible if (window := self._symbol_window(symbol)) is not None
                and len(window) >= self.lookback and window.index[-1] == self.context.asof}

    def _observation(self) -> np.ndarray:
        portfolio = self.context.portfolio
        equity = portfolio.equity
        eligible = self._entry_symbols()
        features = []
        for symbol in self.backtest_spec.symbols:
            window = self._symbol_window(symbol)
            weight = portfolio.market_values.get(symbol, 0.) / equity if equity else 0.
            shares = portfolio.shares.get(symbol, 0)
            sellable = portfolio.sellable_shares.get(symbol, 0) / shares if shares else 0.
            if window is None or window.empty:
                features.extend([0.] * 10 + [weight, sellable])
                continue
            close = window.close.to_numpy(dtype=float).copy()
            # Splits known by this close adjust only earlier observations.
            if self.bundle.price_mode == "raw_cash" and not self.bundle.events.empty:
                splits = self.bundle.events.loc[(self.bundle.events.symbol == symbol)
                    & (self.bundle.events.event_type == "split")
                    & (pd.to_datetime(self.bundle.events.ex_date) <= self.context.asof)]
                for event in splits.itertuples():
                    close[window.index < pd.Timestamp(event.ex_date)] /= event.split_ratio
            returns = [math.log(close[-1] / close[max(0, len(close) - 1 - count)]) for count in (5, 20, 60, self.lookback - 1)]
            daily_returns = np.diff(np.log(close[-21:]))
            volatility = float(daily_returns.std()) if daily_returns.size else 0.
            drawdown = close[-1] / close[-self.lookback:].max() - 1.
            amount = window.amount.to_numpy(dtype=float)
            mean_amount = float(amount[-20:].mean())
            amount_ratio = math.log(amount[-1] / mean_amount) if amount[-1] > 0 and mean_amount > 0 else 0.
            last = window.iloc[-1]
            bar_range = float((last.high - last.low) / last.close)
            available = float(window.index[-1] == self.context.asof)
            features.extend(returns + [volatility, drawdown, amount_ratio, bar_range, available,
                                       float(symbol in eligible), weight, sellable])
        reference = max(self.runner.risk.state["risk_reference"], equity)
        drawdown = 1. - equity / reference if reference else 0.
        features.extend([portfolio.cash / equity if equity else 0., portfolio.receivables / equity if equity else 0.,
                         float(self.runner.risk.state["paused"]), drawdown])
        return np.clip(np.asarray(features, dtype=np.float64), -10., 10.).astype(np.float32)

    def _info(self, elapsed=0) -> dict:
        return dict(asof=self.context.asof.date().isoformat(), equity=self.context.portfolio.equity,
                    elapsed_sessions=int(elapsed), episode_start=self.runner.spec.start.isoformat(),
                    episode_end=self.runner.spec.end.isoformat())

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        start = int(self.np_random.choice(self._episode_starts)) if self.training else 0
        length = self.episode_sessions if self.training and self.episode_sessions is not None else len(self.sessions)
        episode = self.backtest_spec.model_copy(update=dict(start=self.sessions[start].date(), end=self.sessions[start + length - 1].date()))
        self.runner = SessionRunner(self.bundle, episode, self.cancelled, collect=self.collect)
        self.decision_dates = decision_dates(self.calendar, episode.start, episode.end, self.frequency)
        self._decision_set = set(self.decision_dates)
        self._done = False
        self._reward_equity = self.backtest_spec.initial_cash
        self._reward_session_index = -1
        self._advance_to_boundary()
        return self._observation(), self._info()

    def _advance_to_boundary(self):
        while True:
            self.context = self.runner.advance_to_close()
            if self.context.asof in self._decision_set:
                return
            self.runner.submit_close_intent(Hold("between policy decisions"))
            if self.context.asof == self.runner.dates[-1]:
                self._done = True
                return

    def step(self, action):
        if not self.action_space.contains(action):
            raise ValueError("Action does not match hold/target weight bins")
        if int(action[0]) == 0:
            intent = Hold("policy hold")
        else:
            weights = {symbol: int(bin_value) / 4. for symbol, bin_value in zip(self.backtest_spec.symbols, action[1:])}
            total = sum(weights.values())
            if total > 1.:
                weights = {symbol: weight / total for symbol, weight in weights.items()}
            intent = TargetPortfolio(weights, {}, {})
        return self._step_intent(intent)

    def step_weights(self, weights: dict[str, float]):
        """Exact targets for frozen comparison strategies using this same account."""
        intent = TargetPortfolio(weights, {}, {})
        validate_intent(intent, set(self.backtest_spec.symbols))
        return self._step_intent(intent)

    def _step_intent(self, intent):
        if self._done or self.runner is None:
            raise RuntimeError("Reset before stepping a completed environment")
        if isinstance(intent, TargetPortfolio):
            eligible = self._entry_symbols()
            portfolio = self.context.portfolio
            # A symbol lacking history/entry eligibility cannot increase exposure;
            # existing shares remain available for hold/reduction/exit decisions.
            weights = {symbol: weight if symbol in eligible else min(weight,
                portfolio.market_values.get(symbol, 0.) / portfolio.equity if portfolio.equity else 0.)
                for symbol, weight in intent.weights.items()}
            intent = TargetPortfolio(weights, intent.reasons, intent.diagnostics)
        self.runner.submit_close_intent(intent)
        self._advance_to_boundary()
        equity = self.context.portfolio.equity
        if equity <= 0 or self._reward_equity <= 0:
            raise ValueError("Positive equity required for log-return reward")
        reward = math.log(equity / self._reward_equity)
        self._reward_equity = equity
        session_index = self.runner.dates.get_loc(self.context.asof)
        elapsed = session_index - self._reward_session_index
        self._reward_session_index = session_index
        return self._observation(), reward, self._done, False, self._info(elapsed)

    def result(self) -> BacktestResult:
        if self.runner is None:
            raise RuntimeError("Reset before requesting environment results")
        return self.runner.result()
