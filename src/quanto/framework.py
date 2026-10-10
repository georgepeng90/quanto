"""Immutable data shared by framework boundaries; no workbench/runtime imports."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
import math
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer, StrictInt, StrictStr, field_validator, model_validator

from quanto.data.contracts import InstrumentId, utc
from quanto.objects import digest, json_bytes


FRAMEWORK_CONTRACT_VERSION = "multi-asset-v1"
Name = Annotated[StrictStr, Field(min_length=1)]
Hash = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveRevision = Annotated[StrictInt, Field(ge=1)]
PriceMode = Literal["raw_cash", "adjusted_research"]
Operation = Literal["fetch", "fixed_backtest", "train", "resume", "policy_backtest", "bind_model"]
ErrorCode = Literal["IDENTITY_UNRESOLVED", "BINDING_MISMATCH", "MIXED_BASKET_UNSUPPORTED",
                    "DEFINITION_UNAVAILABLE", "DATA_REQUIREMENT_UNSATISFIED", "TEMPORAL_EVIDENCE_MISSING",
                    "EXECUTION_CAPABILITY_MISSING", "MODEL_IDENTITY_MISMATCH"]


def freeze_json(value):
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Only finite JSON values are permitted")


def thaw_json(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(item) for item in value]
    return value


JsonMap = Annotated[Mapping[StrictStr, Any], AfterValidator(freeze_json), PlainSerializer(thaw_json, return_type=dict)]


def canonical_hash(value: Mapping[str, object]) -> str:
    """Hash canonical input; callers select which fields belong to their identity."""
    return digest(json_bytes(thaw_json(freeze_json(thaw_json(value)))))


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, validate_default=True)


def validate_members(members: tuple[InstrumentId, ...]) -> tuple[InstrumentId, ...]:
    if not members or len({item.key for item in members}) != len(members):
        raise ValueError("Basket members must be nonempty and unique")
    if any(item.instrument_type == "index" for item in members):
        raise ValueError("Indices are reference assets, not tradable basket members")
    return members


def _content_hash(model: FrozenContract, field: str) -> None:
    expected = canonical_hash(model.model_dump(mode="json", exclude={field}))
    supplied = getattr(model, field)
    if supplied and supplied != expected:
        raise ValueError(f"{field} does not match immutable content")
    object.__setattr__(model, field, expected)


class BasketRevisionRef(FrozenContract):
    basket_id: Name
    revision: PositiveRevision


class FrozenBasketRevision(BasketRevisionRef):
    members: tuple[InstrumentId, ...]
    created_at: datetime
    selection_note: str
    content_hash: str = ""

    _members = field_validator("members")(validate_members)
    _timestamp = field_validator("created_at")(utc)

    @model_validator(mode="after")
    def verify_hash(self):
        _content_hash(self, "content_hash")
        return self

    @property
    def ref(self) -> BasketRevisionRef:
        return BasketRevisionRef(basket_id=self.basket_id, revision=self.revision)


class DefinitionRef(FrozenContract):
    kind: Literal["feature", "learning", "trading"]
    id: Name
    version: Name


class AssetMapping(FrozenContract):
    instrument_id: InstrumentId
    local_symbol: Name
    evidence_ref: Name

    @model_validator(mode="after")
    def tradable_asset(self):
        validate_members((self.instrument_id,))
        return self


class BenchmarkBinding(FrozenContract):
    instrument_id: InstrumentId | None = None
    custom_series_id: Name | None = None
    return_basis: Literal["price_index", "total_return", "other"]
    evidence_ref: Name

    @model_validator(mode="after")
    def single_identity(self):
        if (self.instrument_id is None) == (self.custom_series_id is None):
            raise ValueError("Benchmark needs exactly one instrument or custom series identity")
        return self


class TableSemanticBinding(FrozenContract):
    table: Name
    semantic_id: Name
    version: Name
    schema_definition: JsonMap
    temporal_rules: JsonMap
    evidence_ref: Name
    declaration_hash: str = ""

    @model_validator(mode="after")
    def verify_hash(self):
        _content_hash(self, "declaration_hash")
        return self


class AssetBinding(FrozenContract):
    schema_version: Literal[1] = 1
    dataset_version_id: Name
    dataset_content_hash: Hash
    assets: Annotated[tuple[AssetMapping, ...], Field(min_length=1)]
    benchmark: BenchmarkBinding
    table_semantics: Annotated[tuple[TableSemanticBinding, ...], Field(min_length=1)]
    binding_hash: str = ""

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def complete_identity(self):
        if len({item.instrument_id.key for item in self.assets}) != len(self.assets) or len({item.local_symbol for item in self.assets}) != len(self.assets):
            raise ValueError("Binding canonical and local identities must each be unique")
        if len({item.table for item in self.table_semantics}) != len(self.table_semantics):
            raise ValueError("Bound table names must be unique")
        _content_hash(self, "binding_hash")
        return self

    def resolve(self, members: tuple[InstrumentId, ...]) -> tuple[AssetMapping, ...]:
        validate_members(members)
        by_id = {item.instrument_id.key: item for item in self.assets}
        missing = [item.key for item in members if item.key not in by_id]
        if missing:
            raise ValueError(f"DATA_REQUIREMENT_UNSATISFIED: missing basket assets {missing}")
        return tuple(by_id[item.key] for item in members)

    def validate_symbols(self, dataset_symbols: Iterable[str]) -> None:
        if set(dataset_symbols) != {item.local_symbol for item in self.assets}:
            raise ValueError("BINDING_MISMATCH: binding must cover every dataset asset exactly")


class ResearchInterval(FrozenContract):
    start: date
    end: date
    warmup_start: date

    @model_validator(mode="after")
    def ordered(self):
        if not self.warmup_start <= self.start <= self.end:
            raise ValueError("warmup_start <= start <= end required")
        return self


class RequirementField(FrozenContract):
    name: Name
    dtype: Literal["float", "int", "bool", "string", "date", "datetime"]
    unit: Name
    currency: Name | None = None
    report_basis: Name | None = None


class RequirementItem(FrozenContract):
    requirement_id: Name
    table: Name
    semantic_version: Name
    fields: Annotated[tuple[RequirementField, ...], Field(min_length=1)]
    scope: Literal["basket", "reference"]
    reference_ids: tuple[InstrumentId, ...] = ()
    interval: ResearchInterval
    warmup_sessions: Annotated[StrictInt, Field(ge=0)] = 0
    availability_rule: Name
    price_modes: Annotated[tuple[PriceMode, ...], Field(min_length=1)]
    required: bool
    missing_policy: Literal["reject", "mask"]
    accepted_limitations: tuple[Name, ...] = ()

    @model_validator(mode="after")
    def unique_fields(self):
        if len({item.name for item in self.fields}) != len(self.fields):
            raise ValueError("Requirement field names must be unique")
        if self.scope == "reference" and not self.reference_ids:
            raise ValueError("Reference requirements need explicit identities")
        if self.scope == "basket" and self.reference_ids:
            raise ValueError("Basket requirements cannot override the basket with reference assets")
        return self


DataRequirements = tuple[RequirementItem, ...]


class ResolvedDefinition(FrozenContract):
    ref: DefinitionRef
    implementation_hash: Hash
    effective_params: JsonMap
    declaration: JsonMap


class CapabilityCheck(FrozenContract):
    name: Name
    status: Literal["passed", "blocked"]
    field_path: str = ""
    code: ErrorCode | None = None
    message: str = ""


class CapabilityReport(FrozenContract):
    operation: Operation
    status: Literal["ready", "blocked"]
    request_hash: Hash | None = None
    dataset_version_id: Name | None = None
    input_hashes: JsonMap = Field(default_factory=dict)
    checks: tuple[CapabilityCheck, ...]
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    checker_version: Name
    generated_at: datetime | None = None

    @field_validator("generated_at")
    @classmethod
    def aware_check_time(cls, value):
        return utc(value) if value is not None else None

    @model_validator(mode="after")
    def readiness(self):
        if self.status == "ready" and (self.request_hash is None or any(item.status == "blocked" for item in self.checks)):
            raise ValueError("Ready capability requires request_hash and no blocked checks")
        return self

    @property
    def error_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.code for item in self.checks if item.code is not None))


class ResolvedConfiguration(FrozenContract):
    framework_contract_version: Literal["multi-asset-v1"] = FRAMEWORK_CONTRACT_VERSION
    operation: Literal["fixed_backtest", "train", "resume", "policy_backtest"]
    request: JsonMap
    basket: FrozenBasketRevision
    asset_binding: AssetBinding
    ordered_pairs: tuple[AssetMapping, ...]
    definitions: tuple[ResolvedDefinition, ...]
    requirements: DataRequirements
    internal_backtest_spec: JsonMap
    internal_training_spec: JsonMap | None = None
    request_hash: Hash
    capability_report: CapabilityReport

    @model_validator(mode="after")
    def correlated_input(self):
        if self.capability_report.request_hash != self.request_hash:
            raise ValueError("Capability report request_hash differs from resolved input")
        if self.capability_report.operation != self.operation:
            raise ValueError("Capability operation differs from resolved input")
        if self.ordered_pairs != self.asset_binding.resolve(self.basket.members):
            raise ValueError("BINDING_MISMATCH: resolved order differs from basket")
        if tuple(self.internal_backtest_spec.get("symbols", ())) != tuple(item.local_symbol for item in self.ordered_pairs):
            raise ValueError("BINDING_MISMATCH: internal symbols differ from binding order")
        return self


class ModelFrameworkBinding(FrozenContract):
    schema_version: Literal[1] = 1
    model_id: Name
    origin: Literal["new", "legacy_audited"]
    original_spec: JsonMap
    original_spec_hash: str = ""
    training_configuration: ResolvedConfiguration
    semantic_identity: JsonMap
    evidence: JsonMap
    created_at: datetime
    binding_hash: str = ""

    _timestamp = field_validator("created_at")(utc)

    @model_validator(mode="after")
    def fixed_training_identity(self):
        config = self.training_configuration
        if config.operation != "train" or config.capability_report.status != "ready" or config.internal_training_spec is None:
            raise ValueError("Model binding requires a resolved training configuration")
        expected_spec_hash = canonical_hash(self.original_spec)
        if self.original_spec_hash and self.original_spec_hash != expected_spec_hash:
            raise ValueError("Model original spec hash differs")
        object.__setattr__(self, "original_spec_hash", expected_spec_hash)
        original_symbols = tuple(self.original_spec.get("backtest", {}).get("symbols", ()))
        if original_symbols != tuple(pair.local_symbol for pair in config.ordered_pairs):
            raise ValueError("MODEL_IDENTITY_MISMATCH: original model basket differs from bound order")
        if not self.semantic_identity or not self.evidence:
            raise ValueError("Model binding requires semantic identity and audit evidence")
        _content_hash(self, "binding_hash")
        return self

    @property
    def basket(self) -> FrozenBasketRevision:
        return self.training_configuration.basket

    @property
    def asset_binding(self) -> AssetBinding:
        return self.training_configuration.asset_binding

    @property
    def dataset_version_id(self) -> str:
        return self.asset_binding.dataset_version_id

    @property
    def ordered_pairs(self) -> tuple[AssetMapping, ...]:
        return self.training_configuration.ordered_pairs
