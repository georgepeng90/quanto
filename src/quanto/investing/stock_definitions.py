"""Registered stock price/volume PPO using the shared daily account and encoder."""
from dataclasses import replace
import inspect

from quanto.backtest import engine, ledger, models, orders, metrics
from quanto.backtest.stock_trading import STOCK_TRADING_DEFINITION
from quanto.framework import DefinitionRef, RequirementField, RequirementItem, canonical_hash, thaw_json
from quanto.investing.definitions import (ETF_MARKET_FEATURES, LearningDefinition, _etf_feature_params,
    _etf_market_features, _etf_sources, _etf_encode, _etf_decode, _etf_reward, _etf_schedule, _etf_environment_factory)
from quanto.investing.environment import DailyETFEnv, etf_market_values
from quanto.investing.rewards import DSR_REWARD_VERSION, STATE_LOG_REWARD_VERSION, DifferentialSharpeState, reward_parameters


def _stock_feature_requirements(params, basket, interval):
    if any(member.instrument_type != "stock" for member in basket.members):
        raise ValueError("DEFINITION_UNAVAILABLE: stock features require a stock basket")
    fields = {
        "prices": (("date", "date", "trading_day"), ("symbol", "string", "instrument_id"),
                   ("close", "float", "price"), ("high", "float", "price"), ("low", "float", "price"), ("amount", "float", "CNY")),
        "events": (("symbol", "string", "instrument_id"), ("event_type", "string", "category"),
                   ("ex_date", "date", "trading_day"), ("split_ratio", "float", "ratio")),
    }
    return tuple(RequirementItem(requirement_id=f"stock-learning-{table}", table=table, semantic_version="1",
        fields=tuple(RequirementField(name=name, dtype=dtype, unit=unit,
                     currency="CNY" if table == "prices" and dtype == "float" else None) for name, dtype, unit in columns),
        scope="basket", interval=interval, warmup_sessions=0,
        availability_rule="session_close" if table == "prices" else "execution_event", price_modes=("raw_cash",),
        required=table != "prices", missing_policy="mask" if table == "prices" else "reject") for table, columns in fields.items())


STOCK_MARKET_FEATURES = replace(ETF_MARKET_FEATURES,
    ref=DefinitionRef(kind="feature", id="stock-market-observation", version="1"), supported_instrument_types=("stock",),
    implementation_hash=canonical_hash({"requirements": inspect.getsource(_stock_feature_requirements),
        "compute": inspect.getsource(_etf_market_features), "sources": inspect.getsource(_etf_sources), "arithmetic": inspect.getsource(etf_market_values)}),
    resolve_requirements=_stock_feature_requirements)


STOCK_PPO_DEFINITION = LearningDefinition(
    ref=DefinitionRef(kind="learning", id="stock-ppo-cash-log", version="1"),
    implementation_hash=canonical_hash({"environment": inspect.getsource(DailyETFEnv), "definition": inspect.getsource(LearningDefinition),
        "hooks": [inspect.getsource(hook) for hook in (_etf_feature_params, _stock_feature_requirements, _etf_encode, _etf_decode,
                    _etf_reward, _etf_schedule, _etf_environment_factory)],
        "execution": [inspect.getsource(module) for module in (engine, ledger, models, orders, metrics)],
        "trading": STOCK_TRADING_DEFINITION.definition_hash}),
    parameter_schema={"type": "object", "properties": {
        "lookback": {"type": "integer", "minimum": 3, "maximum": 512},
        "frequency": {"type": "string", "enum": ["weekly", "monthly"]},
        "action_version": {"type": "string", "const": "hold-target-cash-quartiles-v2"},
        "reward_version": {"type": "string", "const": "net-log-return-v1"}},
        "required": ["lookback", "frequency", "action_version", "reward_version"], "additionalProperties": False},
    feature_definition=STOCK_MARKET_FEATURES, allowed_trading_refs=(STOCK_TRADING_DEFINITION.ref,),
    action_version="hold-target-cash-quartiles-v2", reward_version="net-log-return-v1", algorithm="sb3-ppo-v1",
    feature_params_hook=_etf_feature_params, requirements_hook=_stock_feature_requirements,
    observation_encoder=_etf_encode, action_decoder=_etf_decode, reward_hook=_etf_reward,
    schedule_hook=_etf_schedule, environment_factory=_etf_environment_factory)


def _stock_reward_definition(definition_id: str, reward_version: str) -> LearningDefinition:
    schema = thaw_json(STOCK_PPO_DEFINITION.parameter_schema)
    schema["properties"]["reward_version"]["const"] = reward_version
    return replace(STOCK_PPO_DEFINITION,
        ref=DefinitionRef(kind="learning", id=definition_id, version="1"),
        implementation_hash=canonical_hash({
            "base": STOCK_PPO_DEFINITION.implementation_hash,
            "reward_version": reward_version,
            "reward_state": inspect.getsource(DifferentialSharpeState),
            "reward_parameters": inspect.getsource(reward_parameters),
        }),
        parameter_schema=schema, reward_version=reward_version)


STOCK_PPO_STATE_LOG_DEFINITION = _stock_reward_definition(
    "stock-ppo-cash-state-log", STATE_LOG_REWARD_VERSION)
STOCK_PPO_DSR_DEFINITION = _stock_reward_definition(
    "stock-ppo-cash-dsr", DSR_REWARD_VERSION)
