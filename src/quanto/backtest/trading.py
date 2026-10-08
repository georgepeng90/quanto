"""Frozen execution declarations and controlled ETF account hooks."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import inspect
import math
import re
from types import MappingProxyType

import pandas as pd

from quanto.config import validate_parameter_schema, validate_parameters
from quanto.framework import (DataRequirements, DefinitionRef, FrozenBasketRevision,
                              PriceMode, RequirementField, RequirementItem,
                              ResearchInterval, canonical_hash, freeze_json, thaw_json)
from .models import money


_EVENT_COLUMNS = ("symbol", "event_type", "record_date", "ex_date", "pay_date",
                  "cash_per_share", "split_ratio")
_FEE_INPUT = frozenset(("quantity", "price", "commission_rate", "minimum_commission"))


def _readonly(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_readonly(item) for item in value)
    return value


@dataclass(frozen=True)
class TradingDefinition:
    ref: DefinitionRef
    implementation_hash: str
    supported_instrument_types: tuple[str, ...]
    supported_price_modes: tuple[PriceMode, ...]
    parameter_schema: Mapping[str, object]
    requirements_hook: Callable[[Mapping[str, object], FrozenBasketRevision, ResearchInterval], DataRequirements]
    scalable_costs: tuple[str, ...]
    lot_rule: str
    settlement_rule: str
    order_validity: str
    valuation_assumptions: tuple[str, ...]
    fee_hook: Callable[[Mapping[str, object]], Mapping[str, float]]
    eligibility_hook: Callable[[Mapping[str, object]], Mapping[str, object]]
    event_hook: Callable[[pd.DataFrame], tuple[Mapping[str, object], ...]]
    definition_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.ref.kind != "trading" or not re.fullmatch(r"[0-9a-f]{64}", self.implementation_hash):
            raise ValueError("Trading definition needs a trading ref and implementation hash")
        instrument_types = tuple(self.supported_instrument_types)
        price_modes = tuple(self.supported_price_modes)
        if (not instrument_types or len(set(instrument_types)) != len(instrument_types)
                or set(instrument_types) - {"etf", "stock"}):
            raise ValueError("Trading definition needs supported instrument types")
        if (not price_modes or len(set(price_modes)) != len(price_modes)
                or set(price_modes) - {"raw_cash", "adjusted_research"}):
            raise ValueError("Trading definition needs supported price modes")
        schema = freeze_json(thaw_json(self.parameter_schema))
        validate_parameter_schema(thaw_json(schema))
        for name in ("lot_rule", "settlement_rule", "order_validity"):
            if not getattr(self, name):
                raise ValueError(f"Trading definition needs {name}")
        costs = tuple(self.scalable_costs)
        if len(set(costs)) != len(costs):
            raise ValueError("Scalable costs must be unique")
        object.__setattr__(self, "parameter_schema", schema)
        object.__setattr__(self, "supported_instrument_types", instrument_types)
        object.__setattr__(self, "supported_price_modes", price_modes)
        object.__setattr__(self, "scalable_costs", costs)
        object.__setattr__(self, "valuation_assumptions", tuple(self.valuation_assumptions))
        object.__setattr__(self, "definition_hash", canonical_hash({
            "ref": self.ref.model_dump(mode="json"), "implementation": self.implementation_hash,
            "parameters": schema, "instrument_types": instrument_types,
            "price_modes": price_modes, "scalable_costs": costs,
            "lot_rule": self.lot_rule, "settlement_rule": self.settlement_rule,
            "order_validity": self.order_validity,
            "valuation_assumptions": self.valuation_assumptions,
        }))

    def resolve_requirements(
        self,
        params: Mapping[str, object],
        basket: FrozenBasketRevision,
        interval: ResearchInterval,
    ) -> DataRequirements:
        effective = freeze_json(validate_parameters(thaw_json(self.parameter_schema), thaw_json(params)))
        result = self.requirements_hook(effective, basket, interval)
        if not isinstance(result, tuple) or any(not isinstance(item, RequirementItem) for item in result):
            raise ValueError("Trading requirements must be a tuple of RequirementItem")
        return result

    def fees(self, fill_input: Mapping[str, object]) -> Mapping[str, float]:
        if not isinstance(fill_input, Mapping) or set(fill_input) != _FEE_INPUT:
            raise ValueError("Trading fee input must contain quantity, price, and execution commission settings")
        values = dict(fill_input)
        if type(values["quantity"]) is not int or values["quantity"] < 0:
            raise ValueError("Trading fee quantity must be a nonnegative integer")
        for name in ("price", "commission_rate", "minimum_commission"):
            value = values[name]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (name == "price" and value == 0):
                raise ValueError(f"Trading fee {name} must be finite and nonnegative")
        result = self.fee_hook(MappingProxyType(values))
        if not isinstance(result, Mapping) or set(result) != {"commission"}:
            raise ValueError("Trading fee hook must return commission only")
        commission = result["commission"]
        if type(commission) is not float or not math.isfinite(commission) or commission < 0:
            raise ValueError("Trading commission must be a finite nonnegative float")
        return MappingProxyType({"commission": commission})

    def eligibility(self, state: Mapping[str, object]) -> Mapping[str, object]:
        if not isinstance(state, Mapping) or not {"status", "can_buy", "can_sell"}.issubset(state):
            raise ValueError("EXECUTION_CAPABILITY_MISSING: actual trading status and side flags required")
        values = dict(state)
        if (values["status"] not in {"trading", "suspended"}
                or type(values["can_buy"]) is not bool or type(values["can_sell"]) is not bool
                or (values["status"] == "suspended" and (values["can_buy"] or values["can_sell"]))):
            raise ValueError("EXECUTION_CAPABILITY_MISSING: unsupported or inconsistent trading status")
        result = self.eligibility_hook(_readonly(values))
        if (not isinstance(result, Mapping) or set(result) != {"can_buy", "can_sell", "reason"}
                or type(result["can_buy"]) is not bool or type(result["can_sell"]) is not bool
                or type(result["reason"]) is not str):
            raise ValueError("Trading eligibility hook returned an invalid result")
        if (result["can_buy"] and not values["can_buy"]) or (result["can_sell"] and not values["can_sell"]):
            raise ValueError("EXECUTION_CAPABILITY_MISSING: trading hook cannot grant an unobserved permission")
        return MappingProxyType(dict(result))

    def events(self, rows: pd.DataFrame, *, price_mode: PriceMode) -> tuple[Mapping[str, object], ...]:
        if price_mode not in self.supported_price_modes:
            raise ValueError(f"EXECUTION_CAPABILITY_MISSING: unsupported price mode {price_mode}")
        if not isinstance(rows, pd.DataFrame):
            raise ValueError("Trading events require a DataFrame")
        if not rows.empty:
            missing = set(_EVENT_COLUMNS) - set(rows.columns)
            if missing:
                raise ValueError(f"EXECUTION_CAPABILITY_MISSING: event columns missing {sorted(missing)}")
            unknown = set(rows["event_type"]) - {"dividend", "split"}
            if unknown:
                raise ValueError(f"EXECUTION_CAPABILITY_MISSING: unsupported event {sorted(map(str, unknown))}")
        if price_mode == "adjusted_research":
            return ()
        result = self.event_hook(rows.copy(deep=True))
        if not isinstance(result, tuple):
            raise ValueError("Trading event hook must return a tuple")
        checked = []
        for event in result:
            if not isinstance(event, Mapping) or set(event) != set(_EVENT_COLUMNS):
                raise ValueError("Trading event hook returned an invalid event shape")
            kind = event["event_type"]
            if kind not in {"dividend", "split"}:
                raise ValueError(f"EXECUTION_CAPABILITY_MISSING: unsupported event {kind}")
            if not isinstance(event["symbol"], str) or not event["symbol"]:
                raise ValueError("Trading event needs a symbol")
            required_dates = ("record_date", "ex_date", "pay_date") if kind == "dividend" else ("ex_date",)
            for name in required_dates:
                if not isinstance(event[name], pd.Timestamp) or pd.isna(event[name]):
                    raise ValueError(f"Trading event needs {name}")
            numeric = "cash_per_share" if kind == "dividend" else "split_ratio"
            value = event[numeric]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Trading event needs positive {numeric}")
            checked.append(MappingProxyType(dict(event)))
        return tuple(checked)


def _etf_requirements(
    params: Mapping[str, object], basket: FrozenBasketRevision, interval: ResearchInterval,
) -> DataRequirements:
    if not basket.members or any(item.instrument_type != "etf" for item in basket.members):
        raise ValueError("EXECUTION_CAPABILITY_MISSING: ETF trading needs an ETF basket")
    fields = {
        "prices": (("date", "date", "trading_day"), ("symbol", "string", "instrument_id"),
                   ("open", "float", "price"), ("close", "float", "price")),
        "benchmark": (("date", "date", "trading_day"), ("close", "float", "index_level")),
        "calendar": (("date", "date", "trading_day"),),
        "instruments": (("symbol", "string", "instrument_id"), ("lot_size", "int", "share"),
                        ("settlement_sessions", "int", "session"), ("listing_date", "date", "trading_day")),
        "status": (("date", "date", "trading_day"), ("symbol", "string", "instrument_id"),
                   ("status", "string", "category"), ("can_buy", "bool", "flag"),
                   ("can_sell", "bool", "flag")),
        "events": (("symbol", "string", "instrument_id"), ("event_type", "string", "category"),
                   ("record_date", "date", "trading_day"), ("ex_date", "date", "trading_day"),
                   ("pay_date", "date", "trading_day"), ("cash_per_share", "float", "cash_per_share"),
                   ("split_ratio", "float", "ratio")),
    }
    availability = {"prices": "session_close", "benchmark": "session_close",
                    "calendar": "declared_calendar", "instruments": "declared_static_reference",
                    "status": "session_close", "events": "execution_event"}
    return tuple(RequirementItem(
        requirement_id=f"etf-trading-{table}", table=table, semantic_version="1",
        fields=tuple(RequirementField(name=name, dtype=dtype, unit=unit,
                     currency="CNY" if unit in {"price", "cash_per_share"} else None)
                     for name, dtype, unit in columns),
        scope="basket", interval=interval, availability_rule=availability[table],
        price_modes=("raw_cash", "adjusted_research"), required=True, missing_policy="reject",
    ) for table, columns in fields.items())


def _etf_fees(fill_input: Mapping[str, object]) -> Mapping[str, float]:
    quantity = fill_input["quantity"]
    return {"commission": money(max(fill_input["minimum_commission"],
                                    quantity * fill_input["price"] * fill_input["commission_rate"])) if quantity else 0.0}


def _etf_eligibility(state: Mapping[str, object]) -> Mapping[str, object]:
    if state["status"] == "suspended":
        return {"can_buy": False, "can_sell": False, "reason": "suspended"}
    can_buy, can_sell = state["can_buy"], state["can_sell"]
    reason = "cannot_buy" if not can_buy else "cannot_sell" if not can_sell else ""
    if state.get("order_side") in {"buy", "sell"}:
        side = state["order_side"]
        reason = "" if state[f"can_{side}"] else f"cannot_{side}"
    return {"can_buy": can_buy, "can_sell": can_sell, "reason": reason}


def _etf_events(rows: pd.DataFrame) -> tuple[Mapping[str, object], ...]:
    result = []
    for row in rows.itertuples(index=False):
        event = {name: getattr(row, name) for name in _EVENT_COLUMNS}
        for name in ("record_date", "ex_date", "pay_date"):
            event[name] = None if pd.isna(event[name]) else pd.Timestamp(event[name])
        for name in ("cash_per_share", "split_ratio"):
            event[name] = None if pd.isna(event[name]) else float(event[name])
        result.append(event)
    return tuple(result)


_ETF_IMPLEMENTATION_HASH = canonical_hash({
    "requirements": inspect.getsource(_etf_requirements), "fees": inspect.getsource(_etf_fees),
    "eligibility": inspect.getsource(_etf_eligibility), "events": inspect.getsource(_etf_events),
    "boundary": inspect.getsource(TradingDefinition), "readonly": inspect.getsource(_readonly),
})

ETF_TRADING_DEFINITION = TradingDefinition(
    ref=DefinitionRef(kind="trading", id="etf-daily", version="1"),
    implementation_hash=_ETF_IMPLEMENTATION_HASH,
    supported_instrument_types=("etf",),
    supported_price_modes=("raw_cash", "adjusted_research"),
    parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
    requirements_hook=_etf_requirements,
    scalable_costs=("commission_rate", "minimum_commission", "slippage_rate"),
    lot_rule="instrument.lot_size; full exit may liquidate an odd lot",
    settlement_rule="instrument.settlement_sessions on the declared calendar",
    order_validity="next open only; unfilled quantity expires",
    valuation_assumptions=("close marks with bounded stale sessions", "adjusted research prices include corporate actions"),
    fee_hook=_etf_fees, eligibility_hook=_etf_eligibility, event_hook=_etf_events,
)
