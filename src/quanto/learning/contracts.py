from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from quanto.data.contracts import Contract, DataQuery, utc


class LearningCancelled(RuntimeError):
    pass


def check_cancelled(cancelled):
    if cancelled():
        raise LearningCancelled("learning job cancelled")


def timestamp(value):
    return utc(datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value)


class PrepareSpec(Contract):
    name: str = Field(min_length=1, max_length=120)
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    query: DataQuery
    frequency_minutes: Literal[1, 5, 15, 30, 60] = 1

    @model_validator(mode="after")
    def minute_query(self):
        if self.query.kind != "bars" or self.query.frequency != "1m":
            raise ValueError("learning requires a 1m bars query")
        if not set(("open", "high", "low", "close", "volume", "amount")).issubset(self.query.columns):
            raise ValueError("complete OHLCV and amount columns required")
        return self


class TrainingSpec(Contract):
    name: str = Field(min_length=1, max_length=120)
    model_type: Literal["linear", "gru"] = "gru"
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime
    lookback: int = Field(default=32, ge=1, le=512)
    horizon: int = Field(default=5, ge=1, le=512)
    epochs: int = Field(default=20, ge=1, le=1000)
    batch_size: int = Field(default=256, ge=1, le=4096)
    learning_rate: float = Field(default=.001, gt=0, le=1)
    hidden_size: int = Field(default=32, ge=1, le=512)
    layers: int = Field(default=1, ge=1, le=4)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    patience: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def chronological(self):
        names = ("train_start", "train_end", "validation_start", "validation_end", "test_start", "test_end")
        values = [utc(getattr(self, name)) for name in names]
        for name, value in zip(names, values):
            object.__setattr__(self, name, value)
        if not (values[0] < values[1] <= values[2] < values[3] <= values[4] < values[5]):
            raise ValueError("train, validation, test intervals must be strictly ordered and nonoverlapping")
        return self


def parse_prepare_spec(spec):
    return PrepareSpec.model_validate(spec).model_dump(mode="json")


def parse_training_spec(spec):
    return TrainingSpec.model_validate(spec).model_dump(mode="json")
