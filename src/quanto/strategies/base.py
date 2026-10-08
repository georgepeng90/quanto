from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import copy
import math
from types import MappingProxyType
from typing import Mapping, Protocol

import pandas as pd

from quanto.config import validate_parameter_schema, validate_parameters
from quanto.framework import (DataRequirements, DefinitionRef, FrozenBasketRevision,
                              RequirementItem, ResearchInterval, canonical_hash, freeze_json, thaw_json)


def _readonly(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_readonly(item) for item in value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class PortfolioView:
    cash: float
    receivables: float
    shares: Mapping[str, int]
    sellable_shares: Mapping[str, int]
    market_values: Mapping[str, float]
    equity: float
    stale_value: float

    def __post_init__(self) -> None:
        for name in ("shares", "sellable_shares", "market_values"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class StrategyContext:
    asof: pd.Timestamp
    history: pd.DataFrame
    eligible_symbols: tuple[str, ...]
    portfolio: PortfolioView
    pending_orders: tuple[Mapping, ...]
    risk_state: Mapping
    is_week_end: bool
    data_view: object | None = None
    features: object | None = None
    instruments: Mapping[str, Mapping[str, object]] | None = None

    def __post_init__(self) -> None:
        asof = pd.Timestamp(self.asof)
        if "date" not in self.history:
            raise ValueError("Strategy history requires a date column")
        dates = self.history["date"]
        if not pd.api.types.is_datetime64_any_dtype(dates.dtype):
            dates = pd.to_datetime(dates)
        history = self.history.loc[dates <= asof].copy(deep=True)
        object.__setattr__(self, "asof", asof)
        object.__setattr__(self, "history", history)
        object.__setattr__(self, "eligible_symbols", tuple(self.eligible_symbols))
        object.__setattr__(self, "pending_orders", _readonly(self.pending_orders))
        object.__setattr__(self, "risk_state", _readonly(self.risk_state))
        if self.data_view is not None:
            if self.data_view.asof != asof.date():
                raise ValueError("Data view asof differs from strategy date")
            if pd.Timestamp(self.data_view.signal_cutoff).tz_convert("Asia/Shanghai").date() != asof.date():
                raise ValueError("Data view cutoff differs from strategy date")
            if tuple(self.data_view.ordered_symbols) != tuple(self.instruments or self.data_view.ordered_symbols):
                raise ValueError("Instrument order differs from data view")
        if self.features is not None:
            if self.data_view is None:
                raise ValueError("Features require a bound data view")
            if self.features.asof != self.data_view.asof or self.features.signal_cutoff != self.data_view.signal_cutoff:
                raise ValueError("Feature asof or cutoff differs from data view")
            if tuple(self.features.ordered_symbols) != tuple(self.data_view.ordered_symbols):
                raise ValueError("Feature order differs from data view")
            if self.features.dataset_content_hash != self.data_view.dataset_content_hash:
                raise ValueError("Feature dataset differs from data view")
            if self.features.binding_hash != self.data_view.binding_hash:
                raise ValueError("Feature binding differs from data view")
        object.__setattr__(self, "instruments", _readonly(self.instruments) if self.instruments is not None else None)


@dataclass(frozen=True)
class FixedStrategyDefinition:
    manifest: Mapping[str, object]
    implementation_ref: str
    implementation_hash: str
    supported_instrument_types: tuple[str, ...] = field(init=False)
    parameter_schema: Mapping[str, object] = field(init=False)
    feature_set_ref: DefinitionRef | None = field(init=False)
    feature_param_map: Mapping[str, str] = field(init=False)
    allowed_trading_refs: tuple[DefinitionRef, ...] = field(init=False)
    definition_hash: str = field(init=False)

    def __post_init__(self) -> None:
        import re

        manifest = freeze_json(thaw_json(self.manifest))
        if (not self.implementation_ref or self.implementation_ref != manifest.get("entrypoint")
                or not re.fullmatch(r"[0-9a-f]{64}", self.implementation_hash)):
            raise ValueError("Fixed strategy implementation identity is invalid")
        types = tuple(manifest.get("supported_instrument_types", ()))
        if not types or len(set(types)) != len(types) or set(types) - {"etf", "stock"}:
            raise ValueError("Fixed strategy needs supported instrument types")
        schema = manifest.get("parameter_schema")
        if not isinstance(schema, Mapping) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("Fixed strategy needs a strict parameter schema")
        validate_parameter_schema(thaw_json(schema))
        if "feature_set_ref" not in manifest or "feature_param_map" not in manifest:
            raise ValueError("Fixed strategy requires explicit feature_set_ref and feature_param_map")
        feature_raw = manifest.get("feature_set_ref")
        feature = DefinitionRef(**feature_raw) if feature_raw is not None else None
        if feature is not None and feature.kind != "feature":
            raise ValueError("Shared feature reference must be a feature definition")
        param_map = manifest.get("feature_param_map")
        if not isinstance(param_map, Mapping) or (feature is None and param_map):
            raise ValueError("Feature parameter mapping needs one shared feature reference")
        host_params = set(schema.get("properties", {}))
        if len(set(param_map.values())) != len(param_map) or set(param_map.values()) - host_params:
            raise ValueError("Feature parameters need unique declared host sources")
        trading = tuple(DefinitionRef(**item) for item in manifest.get("allowed_trading_refs", ()))
        if not trading or any(item.kind != "trading" for item in trading) or len(set(trading)) != len(trading):
            raise ValueError("Fixed strategy needs distinct allowed trading definitions")
        if not manifest.get("data_requirements"):
            raise ValueError("Fixed strategy needs explicit data requirements")
        requirement_ids = set()
        shape_interval = ResearchInterval(start=date.min, end=date.min, warmup_start=date.min)
        for declaration in manifest["data_requirements"]:
            if not isinstance(declaration, Mapping):
                raise ValueError("Fixed strategy requirements must be objects")
            windows = declaration.get("warmup_parameters", ())
            if (not isinstance(windows, tuple) or len(set(windows)) != len(windows)
                    or any(name not in schema.get("properties", {})
                           or schema["properties"][name].get("type") != "integer" for name in windows)):
                raise ValueError("Warmup parameters must name distinct integer strategy parameters")
            shape = {key: thaw_json(value) for key, value in declaration.items() if key != "warmup_parameters"}
            shape["interval"] = shape_interval
            requirement = RequirementItem(**shape)
            if requirement.requirement_id in requirement_ids:
                raise ValueError("Fixed strategy requirement IDs must be unique")
            requirement_ids.add(requirement.requirement_id)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "supported_instrument_types", types)
        object.__setattr__(self, "parameter_schema", schema)
        object.__setattr__(self, "feature_set_ref", feature)
        object.__setattr__(self, "feature_param_map", MappingProxyType(dict(param_map)))
        object.__setattr__(self, "allowed_trading_refs", trading)
        object.__setattr__(self, "definition_hash", canonical_hash({"manifest": manifest,
            "implementation_ref": self.implementation_ref, "implementation_hash": self.implementation_hash}))

    def resolve_requirements(
        self, params: Mapping[str, object], basket: FrozenBasketRevision, interval: ResearchInterval,
    ) -> DataRequirements:
        effective = validate_parameters(thaw_json(self.parameter_schema), thaw_json(params))
        if any(member.instrument_type not in self.supported_instrument_types for member in basket.members):
            raise ValueError("Fixed strategy does not support basket instrument type")
        requirements = []
        for declaration in self.manifest["data_requirements"]:
            windows = declaration.get("warmup_parameters", ())
            if any(name not in effective or type(effective[name]) is not int or effective[name] < 0 for name in windows):
                raise ValueError("Warmup parameters must be nonnegative integer strategy parameters")
            values = {key: thaw_json(value) for key, value in declaration.items() if key != "warmup_parameters"}
            values["interval"] = interval
            values["warmup_sessions"] = max((int(effective[name]) for name in windows), default=values.get("warmup_sessions", 0))
            requirements.append(RequirementItem(**values))
        if len({item.requirement_id for item in requirements}) != len(requirements):
            raise ValueError("Fixed strategy requirement IDs must be unique")
        return tuple(requirements)


@dataclass(frozen=True)
class Hold:
    reason: str


@dataclass(frozen=True)
class TargetPortfolio:
    weights: dict[str, float]
    reasons: dict[str, str]
    diagnostics: dict


class Strategy(Protocol):
    warmup_bars: int

    def should_evaluate(self, ctx: StrategyContext) -> bool: ...

    def on_close(self, ctx: StrategyContext) -> Hold | TargetPortfolio: ...


def validate_intent(intent: Hold | TargetPortfolio, known_symbols: set[str]) -> None:
    if isinstance(intent, Hold):
        return
    if not isinstance(intent, TargetPortfolio):
        raise ValueError("Strategy must return Hold or TargetPortfolio")
    if not isinstance(intent.weights, dict):
        raise ValueError("Target weights must be a mapping with unique symbols")
    if not set(intent.weights).issubset(known_symbols):
        raise ValueError("Target contains unknown symbols")
    if any(not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(weight) or weight < 0 for weight in intent.weights.values()):
        raise ValueError("Target weights must be finite and nonnegative")
    if sum(intent.weights.values()) > 1 + 1e-12:
        raise ValueError("Target weights exceed one")


def _calendar_dates(calendar: pd.Series | pd.DatetimeIndex | list[date]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(calendar)).sort_values().unique()


def is_week_end(calendar: pd.Series | pd.DatetimeIndex | list[date], asof: date | pd.Timestamp) -> bool:
    dates = _calendar_dates(calendar)
    asof = pd.Timestamp(asof)
    index = dates.searchsorted(asof)
    if index >= len(dates) or dates[index] != asof:
        raise ValueError("asof must be a calendar session")
    return bool(index + 1 < len(dates) and dates[index + 1].to_period("W-FRI") != asof.to_period("W-FRI"))


def validate_warmup_window(
    strategy: Strategy,
    observed_dates: pd.Series | pd.DatetimeIndex | list[date],
    start: date | pd.Timestamp,
    end: date | pd.Timestamp,
) -> None:
    """Validate an opportunity using observed price dates, not empty sessions."""
    dates = _calendar_dates(observed_dates)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    for index, asof in enumerate(dates):
        if index >= strategy.warmup_bars and start <= asof <= end:
            return
    raise ValueError("No strategy session remains after warmup")
