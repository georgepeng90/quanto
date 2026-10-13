"""Ordinary Shanghai/Shenzhen main-board stock execution, using frozen fee facts."""
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import inspect
import math
from types import MappingProxyType

from quanto.data.contracts import InstrumentId
from quanto.framework import DefinitionRef, RequirementField, RequirementItem, canonical_hash
from . import stock_data
from .trading import TradingDefinition, _etf_eligibility, _etf_events, _etf_fees


def _stock_requirements(params, basket, interval):
    if any(member.instrument_type != "stock" or member.exchange not in {"SSE", "SZSE"} for member in basket.members):
        raise ValueError("EXECUTION_CAPABILITY_MISSING: stock trading needs Shanghai/Shenzhen stocks")
    fields = {
        "prices": (("open", "float", "price"), ("close", "float", "price")),
        "benchmark": (("close", "float", "index_level"),),
        "calendar": (("date", "date", "trading_day"),),
        "instruments": (("board", "string", "category"), ("lot_size", "int", "share"),
                        ("settlement_sessions", "int", "session"), ("listing_date", "date", "trading_day")),
        "status": (("status", "string", "category"), ("can_buy", "bool", "flag"), ("can_sell", "bool", "flag")),
        "events": (("event_type", "string", "category"), ("cash_per_share", "float", "cash_per_share"),
                   ("split_ratio", "float", "ratio")),
        **{name: (("source_ref", "string", "reference"),) for name in ("fee_rules", "stock_open_status", "stock_sources")},
    }
    availability = {"prices": "session_close", "benchmark": "session_close", "calendar": "declared_calendar",
                    "instruments": "declared_static_reference", "status": "session_close"}
    return tuple(RequirementItem(requirement_id=f"stock-trading-{table}", table=table, semantic_version="1",
        fields=tuple(RequirementField(name=name, dtype=dtype, unit=unit,
                     currency="CNY" if unit in {"price", "cash_per_share"} else None) for name, dtype, unit in columns),
        scope="basket", interval=interval, availability_rule=availability.get(table, "execution_event"),
        price_modes=("raw_cash",), required=True, missing_policy="reject") for table, columns in fields.items())


def _cents(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class StockTradingDefinition(TradingDefinition):
    cost_multiplier: float = 1.0

    def with_cost_multiplier(self, multiplier: float):
        if type(multiplier) not in (int, float) or not math.isfinite(multiplier) or multiplier <= 0:
            raise ValueError("Stock cost multiplier must be positive and finite")
        return replace(self, cost_multiplier=float(multiplier))

    def fees(self, fill_input: Mapping) -> Mapping[str, float]:
        names = {"trade_date", "instrument_id", "board", "side", "quantity", "price", "commission_rate", "minimum_commission", "fee_rule"}
        if not isinstance(fill_input, Mapping) or set(fill_input) != names:
            raise ValueError("Stock fees require date, identity, side and frozen fee rule")
        values, rule = fill_input, fill_input["fee_rule"]
        if not isinstance(rule, Mapping) or not {"exchange", "instrument_type", "board", "side", "effective_from", "effective_to",
                "stamp_duty_rate", "transfer_fee_rate"}.issubset(rule):
            raise ValueError("Stock fees require a complete frozen fee rule")
        try:
            exchange, kind, code = values["instrument_id"].split(":")
            InstrumentId(exchange=exchange, instrument_type=kind, code=code)
        except (AttributeError, ValueError) as exc:
            raise ValueError("Stock fee identity is invalid") from exc
        day = values["trade_date"]
        if (type(day) is not date or not rule["effective_from"] <= day < rule["effective_to"]
                or exchange not in {"SSE", "SZSE"} or kind != "stock" or values["board"] != "main_board"
                or any(rule[key] != value for key, value in (("exchange", exchange), ("instrument_type", kind),
                            ("board", values["board"]), ("side", values["side"]))) or values["side"] not in {"buy", "sell"}):
            raise ValueError("Stock fee rule does not match fill identity/date/side")
        for key in ("stamp_duty_rate", "transfer_fee_rate"):
            rate = rule[key]
            if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
                raise ValueError("Invalid stock fee rate")
        if values["side"] == "buy" and rule["stamp_duty_rate"] != 0:
            raise ValueError("Buy stamp duty must be zero")
        commission = super().fees({key: values[key] for key in ("quantity", "price", "commission_rate", "minimum_commission")})["commission"]
        notional = values["quantity"] * values["price"]
        return MappingProxyType(dict(commission=commission,
            stamp_duty=_cents(notional * rule["stamp_duty_rate"]) * self.cost_multiplier,
            transfer_fee=_cents(notional * rule["transfer_fee_rate"]) * self.cost_multiplier))


STOCK_TRADING_DEFINITION = StockTradingDefinition(
    ref=DefinitionRef(kind="trading", id="stock-daily", version="1"),
    implementation_hash=canonical_hash({"stock": inspect.getsource(StockTradingDefinition),
        "requirements": inspect.getsource(_stock_requirements), "data": inspect.getsource(stock_data),
        "rounding": inspect.getsource(_cents), "commission": inspect.getsource(_etf_fees),
        "boundary": inspect.getsource(TradingDefinition),
        "eligibility": inspect.getsource(_etf_eligibility), "events": inspect.getsource(_etf_events)}),
    supported_instrument_types=("stock",), supported_price_modes=("raw_cash",),
    parameter_schema={"type": "object", "properties": {}, "additionalProperties": False}, requirements_hook=_stock_requirements,
    scalable_costs=("commission_rate", "minimum_commission", "slippage_rate"),
    lot_rule="100 shares; full exits may sell integer odd lots", settlement_rule="T+1 sellable shares",
    order_validity="next session open only; expire unfilled quantity",
    valuation_assumptions=(stock_data.STOCK_DIVIDEND_LIMITATION,
        "Stamp duty and transfer fees round half up to CNY cents per fill before experimental cost scaling.",),
    fee_hook=_etf_fees, eligibility_hook=_etf_eligibility, event_hook=_etf_events)
