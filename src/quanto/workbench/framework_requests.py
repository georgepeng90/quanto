"""Strict public inputs; only the server derives runtime-local symbols."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from quanto.config import BacktestSpec, ExecutionSpec, MetricSpec, RiskSpec, StrictSpec
from quanto.data.contracts import InstrumentId
from quanto.framework import (AssetMapping, BasketRevisionRef, DefinitionRef, JsonMap, Name,
                              PositiveRevision, PriceMode, validate_members)
from quanto.investing.contracts import CASH_ACTION_VERSION, PolicyBacktestSpec, PolicyTrainingSpec


class BacktestInput(StrictSpec):
    start: date
    end: date
    initial_cash: float = Field(gt=0)
    seed: StrictInt
    execution: ExecutionSpec
    risk: RiskSpec
    metrics: MetricSpec

    @model_validator(mode="after")
    def ordered(self):
        if self.start > self.end:
            raise ValueError("start must not follow end")
        return self

    def to_spec(self, pairs: tuple[AssetMapping, ...]) -> BacktestSpec:
        values = self.model_dump(mode="json")
        mapping = {item.instrument_id.key: item.local_symbol for item in pairs}
        for scenario in values["risk"]["stress_scenarios"]:
            if set(scenario["symbol_shocks"]) - mapping.keys():
                raise ValueError("Risk assets must be canonical identities from the selected basket")
            scenario["symbol_shocks"] = {mapping[key]: value for key, value in scenario["symbol_shocks"].items()}
        return BacktestSpec.model_validate({**values, "symbols": [item.local_symbol for item in pairs]})


class TrainingInput(PolicyTrainingSpec):
    backtest: BacktestInput
    action_version: Literal["hold-target-quartiles-v1", "hold-target-cash-quartiles-v2"] = CASH_ACTION_VERSION

    def to_spec(self, pairs: tuple[AssetMapping, ...]) -> PolicyTrainingSpec:
        values = self.model_dump(mode="json")
        values["backtest"] = self.backtest.to_spec(pairs).model_dump(mode="json")
        return PolicyTrainingSpec.model_validate(values)


def _kind(ref: DefinitionRef, expected: str) -> DefinitionRef:
    if ref.kind != expected:
        raise ValueError(f"Expected {expected} definition kind")
    return ref


class SourceExperimentRef(StrictSpec):
    experiment_id: Name
    revision: PositiveRevision


class FixedRequest(StrictSpec):
    framework_contract_version: Literal["multi-asset-v1"]
    basket_revision_ref: BasketRevisionRef
    dataset_version_id: Name
    strategy_revision_id: Name
    trading_definition_ref: DefinitionRef
    strategy_params: JsonMap
    backtest_spec: BacktestInput
    sample_purpose: Name
    source_run_id: Name | None = None
    source_experiment_ref: SourceExperimentRef | None = None

    @field_validator("trading_definition_ref")
    @classmethod
    def trading_kind(cls, value):
        return _kind(value, "trading")


class ModelCreateRequest(StrictSpec):
    framework_contract_version: Literal["multi-asset-v1"]
    basket_revision_ref: BasketRevisionRef
    dataset_id: Name
    learning_definition_ref: DefinitionRef
    trading_definition_ref: DefinitionRef
    spec: TrainingInput

    @field_validator("learning_definition_ref", "trading_definition_ref")
    @classmethod
    def definition_kind(cls, value, info):
        return _kind(value, "learning" if info.field_name == "learning_definition_ref" else "trading")


class FetchRequest(StrictSpec):
    instruments: tuple[InstrumentId, ...]
    start_date: date
    end_date: date
    historical_start: date | None = None
    price_mode: PriceMode = "adjusted_research"
    benchmark: InstrumentId = InstrumentId(exchange="SSE", instrument_type="index", code="000300")
    auxiliary: JsonMap = Field(default_factory=dict)

    _members = field_validator("instruments")(validate_members)

    @field_validator("benchmark")
    @classmethod
    def supported_benchmark(cls, value):
        if value.instrument_type != "index":
            raise ValueError("Daily market fetch currently supports an index benchmark only")
        return value

    @model_validator(mode="after")
    def date_range(self):
        if not (self.historical_start or self.start_date) <= self.start_date <= self.end_date:
            raise ValueError("Invalid history or data range")
        return self


class ResumeBody(StrictSpec):
    additional_timesteps: Annotated[StrictInt, Field(ge=2, le=10000000)] | None = None
    device: Literal["cpu", "cuda"] | None = None


class ResumeRequest(ResumeBody):
    model_id: Name


class PolicyBacktestInput(PolicyBacktestSpec):
    evaluation_purpose: Literal["development_replay", "post_selection_backtest"]


class PolicyBacktestRequest(StrictSpec):
    version_id: Name
    dataset_id: Name
    spec: PolicyBacktestInput


class BasketCreateRequest(StrictSpec):
    name: Name
    members: tuple[InstrumentId, ...]
    selection_note: str
    idempotency_key: Name

    _members = field_validator("members")(validate_members)


class BasketRevisionRequest(StrictSpec):
    expected_revision: PositiveRevision
    members: tuple[InstrumentId, ...]
    selection_note: str

    _members = field_validator("members")(validate_members)


class DatasetBindingRequest(StrictSpec):
    evidence_ref: Name
    idempotency_key: Name


class ModelBindingRequest(StrictSpec):
    idempotency_key: Name


class FixedCapabilityRequest(FixedRequest):
    operation: Literal["fixed_backtest"]


class TrainCapabilityRequest(ModelCreateRequest):
    operation: Literal["train"]


class FetchCapabilityRequest(FetchRequest):
    operation: Literal["fetch"]


class ResumeCapabilityRequest(ResumeRequest):
    operation: Literal["resume"]


class BacktestCapabilityRequest(PolicyBacktestRequest):
    operation: Literal["policy_backtest"]
    model_id: Name


FrameworkRequest = Annotated[FixedCapabilityRequest | TrainCapabilityRequest | FetchCapabilityRequest |
                             ResumeCapabilityRequest | BacktestCapabilityRequest, Field(discriminator="operation")]
CapabilityCheckRequest = FrameworkRequest
