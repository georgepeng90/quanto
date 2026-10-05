"""Fixed reward identities and causal daily differential Sharpe moments."""
from __future__ import annotations

import numpy as np


LEGACY_REWARD_VERSION = "net-log-return-v1"
STATE_LOG_REWARD_VERSION = "net-log-return-with-state-v1"
DSR_REWARD_VERSION = "differential-sharpe-v1"
STATE_REWARD_VERSIONS = frozenset({STATE_LOG_REWARD_VERSION, DSR_REWARD_VERSION})


def reward_parameters(reward_version: str) -> dict:
    if reward_version == LEGACY_REWARD_VERSION:
        return {}
    if reward_version not in STATE_REWARD_VERSIONS:
        raise ValueError("unknown reward version")
    return dict(eta=.01, initial_mean=0., initial_second_moment=.0001, variance_floor=1e-8)


class DifferentialSharpeState:
    """Episode-local float64 moments; evaluate DSR before updating them."""

    def __init__(self):
        self.mean = np.float64(0.)
        self.second_moment = np.float64(.0001)

    def update(self, daily_return: float) -> tuple[float, bool]:
        value = np.float64(daily_return)
        if not np.isfinite(value):
            raise ValueError("Daily return must be finite")
        try:
            with np.errstate(over="raise", invalid="raise", divide="raise"):
                delta_mean = value - self.mean
                delta_second = value * value - self.second_moment
                variance = self.second_moment - self.mean * self.mean
                floored = bool(variance < 1e-8)
                denominator = max(variance, np.float64(1e-8)) ** np.float64(1.5)
                reward = (self.second_moment * delta_mean - .5 * self.mean * delta_second) / denominator
                mean = self.mean + .01 * delta_mean
                second_moment = self.second_moment + .01 * delta_second
        except FloatingPointError as exc:
            raise ValueError("DSR reward and moments must remain finite") from exc
        if not all(np.isfinite(item) for item in (reward, mean, second_moment)):
            raise ValueError("DSR reward and moments must remain finite")
        self.mean, self.second_moment = mean, second_moment
        return float(reward), floored
