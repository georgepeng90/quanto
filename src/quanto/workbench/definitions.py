"""Explicit local definitions; versions never fall back to another implementation."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from quanto.backtest.trading import ETF_TRADING_DEFINITION, TradingDefinition
from quanto.config import validate_parameters
from quanto.factors.features import FeatureSetDefinition
from quanto.framework import DefinitionRef, ResolvedDefinition, freeze_json, thaw_json
from quanto.investing.definitions import ETF_MARKET_FEATURES, LearningDefinition, etf_learning_definitions


Definition = FeatureSetDefinition | LearningDefinition | TradingDefinition


def _declaration(definition: Definition) -> Mapping[str, object]:
    values = dict(ref=definition.ref.model_dump(mode="json"),
                  implementation_hash=definition.implementation_hash,
                  definition_hash=definition.definition_hash,
                  supported_instrument_types=definition.supported_instrument_types,
                  parameter_schema=thaw_json(definition.parameter_schema))
    if isinstance(definition, FeatureSetDefinition):
        values.update(fields=[vars(field) for field in definition.fields],
                      transform_recipe=definition.transform_recipe, requires_fit=definition.fit is not None)
    elif isinstance(definition, LearningDefinition):
        values.update(feature_set_ref=definition.feature_definition.ref.model_dump(mode="json"),
                      feature_definition_hash=definition.feature_definition.definition_hash,
                      feature_parameter_source=definition.feature_params_hook.__qualname__,
                      allowed_trading_refs=[ref.model_dump(mode="json") for ref in definition.allowed_trading_refs],
                      action_version=definition.action_version, reward_version=definition.reward_version,
                      algorithm=definition.algorithm)
    else:
        values.update(supported_price_modes=definition.supported_price_modes,
                      scalable_costs=definition.scalable_costs, lot_rule=definition.lot_rule,
                      settlement_rule=definition.settlement_rule, order_validity=definition.order_validity,
                      valuation_assumptions=definition.valuation_assumptions)
    return freeze_json(values)


class DefinitionRegistry:
    def __init__(self, definitions: Iterable[Definition] | None = None):
        definitions = ((ETF_MARKET_FEATURES, *etf_learning_definitions(), ETF_TRADING_DEFINITION)
                       if definitions is None else tuple(definitions))
        registered = {}
        for definition in definitions:
            if not isinstance(definition, (FeatureSetDefinition, LearningDefinition, TradingDefinition)):
                raise TypeError("Only implemented feature, learning and trading definitions can be registered")
            if definition.ref in registered:
                raise ValueError(f"duplicate definition reference: {definition.ref}")
            registered[definition.ref] = definition
        self._definitions = MappingProxyType(registered)

    def get(self, ref: DefinitionRef) -> Definition:
        return self._definitions[ref]

    def list(self) -> tuple[Mapping[str, object], ...]:
        return tuple(_declaration(definition) for definition in self._definitions.values())

    def snapshot(self, definition: Definition, effective_params: Mapping[str, object]) -> ResolvedDefinition:
        registered = self.get(definition.ref)
        if registered.definition_hash != definition.definition_hash:
            raise ValueError("Definition differs from the registered implementation")
        params = validate_parameters(thaw_json(definition.parameter_schema), thaw_json(effective_params))
        return ResolvedDefinition(ref=definition.ref, implementation_hash=definition.implementation_hash,
                                  effective_params=params, declaration=_declaration(definition))
