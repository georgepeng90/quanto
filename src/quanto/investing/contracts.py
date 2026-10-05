from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from quanto.config import BacktestSpec, StrictSpec


LEGACY_ACTION_VERSION = "hold-target-quartiles-v1"
CASH_ACTION_VERSION = "hold-target-cash-quartiles-v2"


class PolicyTrainingSpec(StrictSpec):
    name: str = Field(min_length=1, max_length=120)
    backtest: BacktestSpec
    frequency: Literal["weekly", "monthly"] = "weekly"
    lookback: int = Field(default=120, ge=3, le=512)
    action_version: Literal["hold-target-quartiles-v1", "hold-target-cash-quartiles-v2"] = LEGACY_ACTION_VERSION
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    episode_sessions: int = Field(default=252, ge=2, le=5000)
    total_timesteps: int = Field(default=4096, ge=2, le=10000000)
    n_steps: int = Field(default=128, ge=2, le=8192)
    batch_size: int = Field(default=64, ge=2, le=8192)
    n_epochs: int = Field(default=10, ge=1, le=100)
    learning_rate: float = Field(default=.0003, gt=0, le=1)
    gamma: float = Field(default=1., gt=0, le=1)
    gae_lambda: float = Field(default=.95, gt=0, le=1)
    ent_coef: float = Field(default=.01, ge=0, le=1)
    hidden_size: int = Field(default=64, ge=8, le=512)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    device: Literal["cpu", "cuda"] = "cpu"
    eval_every_timesteps: int = Field(default=512, ge=1, le=10000000)
    selection_metric: Literal["validation_net_log_return", "validation_sharpe"] = "validation_net_log_return"

    @model_validator(mode="after")
    def validate_experiment(self):
        dates = (self.train_start, self.train_end, self.validation_start,
                 self.validation_end, self.test_start, self.test_end)
        if not all(first < second for first, second in zip(dates, dates[1:])):
            raise ValueError("inclusive train, validation and test dates must be strictly ordered and disjoint")
        if self.train_start < self.backtest.start or self.test_end > self.backtest.end:
            raise ValueError("training partitions must be inside backtest coverage")
        if self.batch_size > self.n_steps or self.n_steps % self.batch_size:
            raise ValueError("batch_size must divide n_steps and cannot exceed it")
        return self


class PolicyBacktestSpec(StrictSpec):
    start: date
    end: date
    baseline: Literal["policy", "cash", "equal_weight", "buy_hold"] = "policy"
    cost_multiplier: float = Field(default=1., ge=0, le=100)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start >= self.end:
            raise ValueError("backtest start must precede end")
        return self
