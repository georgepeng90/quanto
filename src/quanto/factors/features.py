"""Immutable, point-in-time market feature contracts."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
import math
from numbers import Real
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from quanto.config import validate_parameters
from quanto.framework import (
    DataRequirements, DefinitionRef, FrozenBasketRevision, ResearchInterval,
    canonical_hash, freeze_json, thaw_json,
)

if TYPE_CHECKING:
    from quanto.data.point_in_time import PointInTimeView


Scope = Literal["per_symbol", "global"]
MissingPolicy = Literal["mask", "reject"]
AgeUnit = Literal["seconds", "hours", "days"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("source and cutoff times must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class FeatureField:
    name: str
    semantic: str
    unit: str
    scope: Scope
    age_unit: AgeUnit
    missing_policy: MissingPolicy
    max_age: float | None = None
    allow_carry_forward: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.semantic or not self.unit:
            raise ValueError("Feature field needs name, semantic and unit")
        if self.scope not in ("per_symbol", "global") or self.age_unit not in ("seconds", "hours", "days"):
            raise ValueError("Invalid feature scope or age unit")
        if self.missing_policy not in ("mask", "reject"):
            raise ValueError("Invalid missing policy")
        if self.max_age is not None and (not isinstance(self.max_age, Real) or not math.isfinite(self.max_age) or self.max_age < 0):
            raise ValueError("max_age must be a nonnegative finite number")
        if type(self.allow_carry_forward) is not bool:
            raise ValueError("allow_carry_forward must be bool")


@dataclass(frozen=True)
class FeatureCell:
    value: float | None
    is_valid: bool
    source_available_at: datetime | None
    age: float | None
    missing_reason: str | None
    source_refs: tuple[str, ...] = ()
    is_carried: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        if type(self.is_valid) is not bool or type(self.is_carried) is not bool:
            raise ValueError("Feature validity and carry marker must be bool")
        if self.is_valid:
            if isinstance(self.value, bool) or not isinstance(self.value, Real) or not math.isfinite(self.value):
                raise ValueError("Valid feature value must be finite")
            object.__setattr__(self, "value", float(self.value))
            if self.source_available_at is None or not self.source_refs:
                raise ValueError("Valid feature value requires a source time and references")
            if self.missing_reason is not None:
                raise ValueError("Valid feature cannot have missing reason")
        elif self.value is not None or not self.missing_reason:
            raise ValueError("Missing feature must have null value and reason")
        if self.source_available_at is not None:
            _utc(self.source_available_at)
        if self.is_carried and (self.source_available_at is None or not self.source_refs):
            raise ValueError("Carried feature requires a source")
        if self.age is not None and (not math.isfinite(self.age) or self.age < 0):
            raise ValueError("Feature age must be finite and nonnegative")
        if any(not isinstance(ref, str) or not ref for ref in self.source_refs):
            raise ValueError("Invalid source reference")


def make_cell(value: float, sources: Sequence[tuple[datetime, str]], *, is_carried: bool = False) -> FeatureCell:
    """Create a value from the exact records used, retaining the latest availability."""
    times = tuple((_utc(moment), ref) for moment, ref in sources)
    if not times:
        raise ValueError("Feature value requires sources")
    return FeatureCell(value, True, max(moment for moment, _ in times), None, None,
                       tuple(ref for _, ref in times), is_carried)


def _missing(reason: str = "missing") -> FeatureCell:
    return FeatureCell(None, False, None, None, reason)


@dataclass(frozen=True)
class FeatureFrame:
    asof: date
    signal_cutoff: datetime
    ordered_symbols: tuple[str, ...]
    per_symbol: Mapping[str, Mapping[str, FeatureCell]]
    global_values: Mapping[str, FeatureCell]
    definition_ref: DefinitionRef
    definition_hash: str
    params_hash: str
    transform_hash: str
    dataset_content_hash: str
    binding_hash: str
    field_schema: tuple[FeatureField, ...]
    frame_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _utc(self.signal_cutoff)
        if self.asof != self.signal_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("Feature frame asof must match signal cutoff Shanghai date")
        symbols = tuple(self.ordered_symbols)
        fields = tuple(self.field_schema)
        if any(not isinstance(symbol, str) or not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("Feature frame symbols must be unique")
        if not fields or len({item.name for item in fields}) != len(fields):
            raise ValueError("Feature frame field schema must be nonempty and unique")
        per_names = tuple(item.name for item in fields if item.scope == "per_symbol")
        global_names = tuple(item.name for item in fields if item.scope == "global")
        if tuple(self.per_symbol) != symbols or tuple(self.global_values) != global_names:
            raise ValueError("Feature frame symbol or global field shape mismatch")
        per_fields = tuple(item for item in fields if item.scope == "per_symbol")
        global_fields = tuple(item for item in fields if item.scope == "global")

        def check_cell(item: FeatureCell, feature: FeatureField) -> None:
            if not isinstance(item, FeatureCell):
                raise ValueError("Feature frame fields must contain cells")
            if item.is_carried and not feature.allow_carry_forward:
                raise ValueError(f"Feature {feature.name} does not permit carry forward")
            if not item.is_valid and feature.missing_policy == "reject":
                raise ValueError(f"Feature {feature.name} missing under reject policy")
            source = item.source_available_at
            if source is None:
                if item.age is not None:
                    raise ValueError(f"Feature {feature.name} age has no source")
                return
            if source > self.signal_cutoff:
                raise ValueError("Feature source exceeds signal cutoff")
            divisor = {"seconds": 1, "hours": 3600, "days": 86400}[feature.age_unit]
            expected = (self.signal_cutoff - source).total_seconds() / divisor
            if item.age is None or not math.isclose(item.age, expected, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError(f"Feature {feature.name} age does not match source and cutoff")
            if item.is_valid and feature.max_age is not None and item.age > feature.max_age:
                raise ValueError(f"Feature {feature.name} exceeds max_age")

        for symbol in symbols:
            if tuple(self.per_symbol[symbol]) != per_names:
                raise ValueError(f"Feature frame field shape mismatch for {symbol}")
            for feature in per_fields:
                check_cell(self.per_symbol[symbol][feature.name], feature)
        for feature in global_fields:
            check_cell(self.global_values[feature.name], feature)
        object.__setattr__(self, "ordered_symbols", symbols)
        object.__setattr__(self, "field_schema", fields)
        per_symbol = MappingProxyType({symbol: MappingProxyType(dict(fields)) for symbol, fields in self.per_symbol.items()})
        global_values = MappingProxyType(dict(self.global_values))
        object.__setattr__(self, "per_symbol", per_symbol)
        object.__setattr__(self, "global_values", global_values)
        object.__setattr__(self, "frame_hash", canonical_hash({
            "asof": self.asof.isoformat(), "cutoff": self.signal_cutoff.isoformat(),
            "symbols": self.ordered_symbols, "per_symbol": {
                symbol: {name: _cell_identity(cell) for name, cell in fields.items()}
                for symbol, fields in per_symbol.items()},
            "global": {name: _cell_identity(cell) for name, cell in global_values.items()},
            "definition": self.definition_hash, "params": self.params_hash,
            "transform": self.transform_hash, "dataset": self.dataset_content_hash,
            "binding": self.binding_hash, "fields": [vars(item) for item in self.field_schema],
        }))


def _cell_identity(cell: FeatureCell) -> dict[str, object]:
    return {"value": cell.value, "valid": cell.is_valid,
            "source": cell.source_available_at.isoformat() if cell.source_available_at else None,
            "age": cell.age, "reason": cell.missing_reason, "refs": cell.source_refs,
            "carried": cell.is_carried}


FeatureRows = tuple[Mapping[str, Mapping[str, FeatureCell]], Mapping[str, FeatureCell]]


@dataclass(frozen=True)
class FeatureSetDefinition:
    ref: DefinitionRef
    implementation_hash: str
    supported_instrument_types: tuple[Literal["etf", "stock"], ...]
    parameter_schema: Mapping[str, object]
    resolve_requirements: Callable[[Mapping[str, object], FrozenBasketRevision, ResearchInterval], DataRequirements]
    fields: tuple[FeatureField, ...]
    compute: Callable[[PointInTimeView, Mapping[str, object]], FeatureRows]
    transform_recipe: str = "raw-v1"
    fit: Callable[[Sequence[FeatureFrame], Mapping[str, object]], Mapping[str, object]] | None = None
    apply: Callable[[FeatureFrame, FrozenTransform, Mapping[str, object]], FeatureFrame] | None = None
    definition_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.ref.kind != "feature" or not re.fullmatch(r"[0-9a-f]{64}", self.implementation_hash) or not self.transform_recipe:
            raise ValueError("Feature definition needs a feature ref, implementation and recipe")
        instrument_types = tuple(self.supported_instrument_types)
        if not instrument_types or len(set(instrument_types)) != len(instrument_types) or set(instrument_types) - {"etf", "stock"}:
            raise ValueError("Feature definition needs supported instrument types")
        if not self.fields or len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("Feature fields must be nonempty and unique")
        if self.fit is not None and self.apply is None:
            raise ValueError("Fitted feature transform needs an apply function")
        schema = freeze_json(thaw_json(self.parameter_schema))
        object.__setattr__(self, "parameter_schema", schema)
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "supported_instrument_types", instrument_types)
        object.__setattr__(self, "definition_hash", canonical_hash({
            "ref": self.ref.model_dump(mode="json"), "implementation": self.implementation_hash,
            "parameters": schema, "fields": [vars(item) for item in self.fields],
            "recipe": self.transform_recipe, "requires_fit": self.fit is not None,
            "supported_instrument_types": instrument_types,
        }))


@dataclass(frozen=True)
class FrozenTransform:
    definition_ref: DefinitionRef
    definition_hash: str
    params_hash: str
    recipe: str
    training_start: date
    training_end: date
    training_cutoff: datetime
    training_frame_hashes: tuple[str, ...]
    training_dataset_hashes: tuple[str, ...]
    training_binding_hashes: tuple[str, ...]
    data: Mapping[str, object]
    transform_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _utc(self.training_cutoff)
        if self.training_end < self.training_start or not self.training_frame_hashes:
            raise ValueError("Transform requires a nonempty training interval")
        if self.training_end != self.training_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date():
            raise ValueError("Transform training_end must match training cutoff Shanghai date")
        frozen = freeze_json(thaw_json(self.data))
        object.__setattr__(self, "data", frozen)
        object.__setattr__(self, "training_frame_hashes", tuple(self.training_frame_hashes))
        object.__setattr__(self, "training_dataset_hashes", tuple(self.training_dataset_hashes))
        object.__setattr__(self, "training_binding_hashes", tuple(self.training_binding_hashes))
        object.__setattr__(self, "transform_hash", canonical_hash({
            "ref": self.definition_ref.model_dump(mode="json"), "definition": self.definition_hash,
            "params": self.params_hash, "recipe": self.recipe,
            "start": self.training_start.isoformat(), "end": self.training_end.isoformat(),
            "cutoff": self.training_cutoff.isoformat(), "frames": self.training_frame_hashes,
            "datasets": self.training_dataset_hashes, "bindings": self.training_binding_hashes,
            "data": frozen,
        }))


def _parameters(definition: FeatureSetDefinition, params: Mapping[str, object]) -> tuple[Mapping[str, object], str]:
    effective = validate_parameters(thaw_json(definition.parameter_schema), thaw_json(params))
    frozen = freeze_json(effective)
    return frozen, canonical_hash(frozen)


def _age(cell: FeatureCell, field: FeatureField, cutoff: datetime) -> FeatureCell:
    if cell.source_available_at is not None and cell.source_available_at > cutoff:
        raise ValueError(f"Feature {field.name} source exceeds signal cutoff")
    if cell.is_carried and not field.allow_carry_forward:
        raise ValueError(f"Feature {field.name} does not permit carry forward")
    age = None
    if cell.source_available_at is not None:
        seconds = (cutoff - cell.source_available_at).total_seconds()
        divisor = {"seconds": 1, "hours": 3600, "days": 86400}[field.age_unit]
        age = seconds / divisor
    if not cell.is_valid:
        if field.missing_policy == "reject":
            raise ValueError(f"Feature {field.name} is missing: {cell.missing_reason}")
        return replace(cell, age=age)
    if field.max_age is not None and age > field.max_age:
        if field.missing_policy == "reject":
            raise ValueError(f"Feature {field.name} exceeds max_age")
        return FeatureCell(None, False, cell.source_available_at, age, "stale", cell.source_refs, cell.is_carried)
    return replace(cell, age=age)


def _validated_frame(
    definition: FeatureSetDefinition, view: PointInTimeView, params_hash: str,
    transform_hash: str, raw: FeatureRows,
) -> FeatureFrame:
    per_symbol_raw, global_raw = raw
    symbols = tuple(view.ordered_symbols)
    if len(set(symbols)) != len(symbols) or set(per_symbol_raw) - set(symbols):
        raise ValueError("Feature output has unknown or duplicate symbol")
    per_fields = tuple(field for field in definition.fields if field.scope == "per_symbol")
    global_fields = tuple(field for field in definition.fields if field.scope == "global")
    if set(global_raw) - {field.name for field in global_fields}:
        raise ValueError("Feature output has unknown global field")
    rows = {}
    for symbol in symbols:
        supplied = per_symbol_raw.get(symbol, {})
        if set(supplied) - {field.name for field in per_fields}:
            raise ValueError(f"Feature output has unknown field for {symbol}")
        rows[symbol] = {field.name: _age(supplied.get(field.name, _missing()), field, view.signal_cutoff)
                        for field in per_fields}
    global_values = {field.name: _age(global_raw.get(field.name, _missing()), field, view.signal_cutoff)
                     for field in global_fields}
    return FeatureFrame(view.asof, view.signal_cutoff, symbols, rows, global_values,
                        definition.ref, definition.definition_hash, params_hash, transform_hash,
                        view.dataset_content_hash, view.binding_hash, definition.fields)


def _raw_fit_hash(definition: FeatureSetDefinition, params_hash: str) -> str:
    return canonical_hash({"definition": definition.definition_hash, "params": params_hash, "state": "raw-fit"})


def compute_fit_frame(
    definition: FeatureSetDefinition, view: PointInTimeView, params: Mapping[str, object],
) -> FeatureFrame:
    """Compute an unfitted frame from the exact definition used by fit_transform."""
    if definition.fit is None:
        raise ValueError("Raw fit frame requires a fitted transform recipe")
    effective, params_hash = _parameters(definition, params)
    return _validated_frame(definition, view, params_hash, _raw_fit_hash(definition, params_hash),
                            definition.compute(view, effective))


def compute_features(
    definition: FeatureSetDefinition, view: PointInTimeView,
    params: Mapping[str, object], transform: FrozenTransform | None,
) -> FeatureFrame:
    """Compute market features for one bound view; never fit a transform."""
    effective, params_hash = _parameters(definition, params)
    _utc(view.signal_cutoff)
    if definition.fit is not None:
        if transform is None:
            raise ValueError("Feature definition requires a fitted transform")
        if (transform.definition_ref != definition.ref or transform.definition_hash != definition.definition_hash
                or transform.params_hash != params_hash or transform.recipe != definition.transform_recipe):
            raise ValueError("Transform definition or parameter identity mismatch")
        transform_hash = transform.transform_hash
    elif transform is not None:
        raise ValueError("Unexpected fitted transform for fixed recipe")
    else:
        transform_hash = canonical_hash({"definition": definition.definition_hash, "recipe": definition.transform_recipe,
                                         "params": params_hash})
    raw = definition.compute(view, effective)
    frame = _validated_frame(definition, view, params_hash, transform_hash, raw)
    if definition.fit is not None:
        frame = definition.apply(frame, transform, effective)
        if not isinstance(frame, FeatureFrame):
            raise ValueError("Transform must return a FeatureFrame")
        raw = (frame.per_symbol, frame.global_values)
        frame = _validated_frame(definition, view, params_hash, transform_hash, raw)
    return frame


def fit_transform(
    definition: FeatureSetDefinition, train_frames: Sequence[FeatureFrame],
    params: Mapping[str, object],
) -> FrozenTransform:
    """Fit only when explicitly requested by the training lifecycle."""
    effective, params_hash = _parameters(definition, params)
    if definition.fit is None:
        raise ValueError("Feature definition has no fitted transform recipe")
    frames = tuple(train_frames)
    raw_hash = _raw_fit_hash(definition, params_hash)
    if not frames or any(frame.definition_ref != definition.ref or frame.definition_hash != definition.definition_hash
                         or frame.params_hash != params_hash or frame.transform_hash != raw_hash for frame in frames):
        raise ValueError("Training frames must match feature definition and parameters as raw fit frames")
    per_fields = {field.name for field in definition.fields if field.scope == "per_symbol"}
    global_fields = {field.name for field in definition.fields if field.scope == "global"}
    if any(frame.field_schema != definition.fields or set(frame.global_values) != global_fields
           or any(set(row) != per_fields for row in frame.per_symbol.values())
           for frame in frames):
        raise ValueError("Training frame fields must match feature definition")
    data = definition.fit(frames, effective)
    return FrozenTransform(definition.ref, definition.definition_hash, params_hash,
                           definition.transform_recipe, min(frame.asof for frame in frames),
                           max(frame.asof for frame in frames), max(frame.signal_cutoff for frame in frames),
                           tuple(frame.frame_hash for frame in frames),
                           tuple(frame.dataset_content_hash for frame in frames),
                           tuple(frame.binding_hash for frame in frames), data)


class FeatureCache:
    """Caller cache backed by immutable market views shared across episodes."""

    def __init__(self) -> None:
        self._frames: dict[str, FeatureFrame] = {}

    def get_or_compute(
        self, definition: FeatureSetDefinition, view: PointInTimeView,
        params: Mapping[str, object], transform: FrozenTransform | None,
    ) -> FeatureFrame:
        _, params_hash = _parameters(definition, params)
        key = canonical_hash({
            "dataset": view.dataset_content_hash, "binding": view.binding_hash,
            "definition": definition.definition_hash, "params": params_hash,
            "transform": transform.transform_hash if transform else None,
            "cutoff": view.signal_cutoff.isoformat(), "asof": view.asof.isoformat(),
            "symbols": tuple(view.ordered_symbols),
        })
        if key not in self._frames:
            # The view outlives episode accounts; its frames contain no account state.
            if key not in view._feature_frames:
                view._feature_frames[key] = compute_features(definition, view, params, transform)
            self._frames[key] = view._feature_frames[key]
        return self._frames[key]
