"""Explicit ETF learning combinations and their shared market feature encoder."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
import inspect
import re

import gymnasium as gym
import pandas as pd

from quanto.backtest.dataset_schemas import DatasetBundle
from quanto.backtest.trading import ETF_TRADING_DEFINITION, TradingDefinition
from quanto.config import BacktestSpec, validate_parameter_schema, validate_parameters
from quanto.data.point_in_time import PointInTimeView, TableSemantics
from quanto.factors.features import FeatureField, FeatureSetDefinition, FrozenTransform, make_cell
from quanto.framework import (AssetBinding, DataRequirements, DefinitionRef, FrozenBasketRevision,
                              RequirementField, RequirementItem, ResearchInterval, canonical_hash, freeze_json, thaw_json)
from quanto.investing.contracts import CASH_ACTION_VERSION, LEGACY_ACTION_VERSION, PolicyTrainingSpec
from quanto.investing.environment import (DailyETFEnv, MARKET_FIELDS, decision_dates, etf_market_values)
from quanto.investing.rewards import DSR_REWARD_VERSION, LEGACY_REWARD_VERSION, STATE_LOG_REWARD_VERSION


def _etf_feature_params(config: PolicyTrainingSpec) -> Mapping[str, object]:
    return {"lookback": config.lookback}


def _etf_sources(window: pd.DataFrame, splits: pd.DataFrame, positions: range | tuple[int, ...],
                 *, adjusted_closes: bool) -> tuple[tuple[object, str], ...]:
    selected = tuple(dict.fromkeys(position % len(window) for position in positions))
    sources = [(window.iloc[position].source_available_at.to_pydatetime(), window.iloc[position].source_ref)
               for position in selected]
    if adjusted_closes:
        for event in splits.itertuples(index=False):
            if any(window.index[position] < pd.Timestamp(event.ex_date) for position in selected):
                sources.append((event.source_available_at.to_pydatetime(), event.source_ref))
    return tuple(sources)


def _etf_market_features(view: PointInTimeView, params: Mapping[str, object]):
    lookback = params["lookback"]
    bars = view.bars(view.ordered_symbols, date.min)
    per_symbol = {}
    for symbol in view.ordered_symbols:
        window = bars.loc[bars.symbol == symbol].sort_values("date").set_index("date").tail(max(lookback, 61))
        if window.empty:
            per_symbol[symbol] = {}
            continue
        splits = view.realized_splits((symbol,)) if view.price_mode == "raw_cash" else pd.DataFrame()
        values = etf_market_values(window, splits, lookback=lookback,
                                   asof=pd.Timestamp(view.asof), price_mode=view.price_mode)
        count = len(window)
        horizons = (5, 20, 60, lookback - 1)
        positions = tuple((max(0, count - 1 - horizon), count - 1) for horizon in horizons)
        positions += (tuple(range(max(0, count - 21), count)),
                      tuple(range(max(0, count - lookback), count)),
                      tuple(range(max(0, count - 20), count)), (count - 1,), (count - 1,))
        row = {}
        for index, name in enumerate(MARKET_FIELDS):
            source = _etf_sources(window, splits, positions[index],
                                  adjusted_closes=view.price_mode == "raw_cash" and index < 6)
            row[name] = make_cell(values[index], source)
        per_symbol[symbol] = row
    return per_symbol, {}


def _etf_feature_requirements(params: Mapping[str, object], basket: FrozenBasketRevision,
                              interval: ResearchInterval) -> DataRequirements:
    if any(member.instrument_type != "etf" for member in basket.members):
        raise ValueError("DEFINITION_UNAVAILABLE: ETF market features require an ETF basket")
    requirements = (
        ("prices", "session_close", (("date", "date", "trading_day"), ("symbol", "string", "instrument_id"),
                                      ("close", "float", "price"), ("high", "float", "price"),
                                      ("low", "float", "price"), ("amount", "float", "CNY"))),
        ("events", "execution_event", (("symbol", "string", "instrument_id"),
                                        ("event_type", "string", "category"), ("ex_date", "date", "trading_day"),
                                        ("split_ratio", "float", "ratio"))),
    )
    return tuple(RequirementItem(requirement_id=f"etf-learning-{table}", table=table, semantic_version="1",
                                 fields=tuple(RequirementField(name=name, dtype=dtype, unit=unit,
                                              currency="CNY" if table == "prices" and dtype == "float" else None)
                                              for name, dtype, unit in fields), scope="basket", interval=interval,
                                 warmup_sessions=max(params["lookback"], 61) if table == "prices" else 0,
                                 availability_rule=availability, price_modes=("raw_cash", "adjusted_research"),
                                 required=True, missing_policy="reject")
                 for table, availability, fields in requirements)


def _etf_learning_requirements(params: Mapping[str, object], basket: FrozenBasketRevision,
                               interval: ResearchInterval) -> DataRequirements:
    return _etf_feature_requirements({"lookback": params["lookback"]}, basket, interval)


def _hook_identity(hook: Callable) -> dict[str, str]:
    try:
        source = inspect.getsource(hook)
    except (OSError, TypeError) as exc:
        raise ValueError("Learning hooks require inspectable source for frozen identity") from exc
    return {"module": hook.__module__, "name": hook.__qualname__, "source": source}


_ETF_FEATURE_IMPLEMENTATION_HASH = canonical_hash({
    "compute": inspect.getsource(_etf_market_features), "sources": inspect.getsource(_etf_sources),
    "arithmetic": inspect.getsource(etf_market_values), "requirements": inspect.getsource(_etf_feature_requirements),
})

ETF_MARKET_FEATURES = FeatureSetDefinition(
    ref=DefinitionRef(kind="feature", id="etf-market-observation", version="1"),
    implementation_hash=_ETF_FEATURE_IMPLEMENTATION_HASH, supported_instrument_types=("etf",),
    parameter_schema={"type": "object", "properties": {"lookback": {"type": "integer", "minimum": 3, "maximum": 512}},
                      "required": ["lookback"], "additionalProperties": False},
    resolve_requirements=_etf_feature_requirements,
    fields=tuple(FeatureField(name=name, semantic=name.replace("_", " "), unit="ratio", scope="per_symbol",
                              age_unit="days", missing_policy="mask") for name in MARKET_FIELDS),
    compute=_etf_market_features,
)


def _etf_encode(env: DailyETFEnv):
    return env._encode_etf_observation()


def _etf_decode(env: DailyETFEnv, action):
    return env._decode_etf_action(action)


def _etf_reward(env: DailyETFEnv, interval: dict, equity: float) -> float:
    return env._etf_reward(interval, equity)


def _etf_schedule(calendar, start, end, frequency):
    return decision_dates(calendar, start, end, frequency)


def _etf_environment_factory(definition, bundle, config, transform, *, backtest, training, asset_binding,
                             trading_definition, table_semantics, cancelled, collect):
    return DailyETFEnv(bundle, backtest, frequency=config.frequency, lookback=config.lookback,
                       episode_sessions=config.episode_sessions, training=training,
                       action_version=config.action_version, reward_version=config.reward_version,
                       cancelled=cancelled, collect=collect, learning_definition=definition,
                       asset_binding=asset_binding, trading_definition=trading_definition,
                       feature_definition=definition.feature_definition,
                       feature_params=dict(definition.feature_params(config)), feature_transform=transform,
                       table_semantics=table_semantics)


@dataclass(frozen=True)
class LearningDefinition:
    ref: DefinitionRef
    implementation_hash: str
    parameter_schema: Mapping[str, object]
    feature_definition: FeatureSetDefinition
    allowed_trading_refs: tuple[DefinitionRef, ...]
    action_version: str
    reward_version: str
    algorithm: str
    feature_params_hook: Callable[[PolicyTrainingSpec], Mapping[str, object]]
    requirements_hook: Callable[[Mapping[str, object], FrozenBasketRevision, ResearchInterval], DataRequirements]
    observation_encoder: Callable[[DailyETFEnv], object]
    action_decoder: Callable[[DailyETFEnv, object], object]
    reward_hook: Callable[[DailyETFEnv, dict, float], float]
    schedule_hook: Callable[[object, date, date, str], object]
    environment_factory: Callable[..., gym.Env]
    definition_hash: str = field(init=False)

    @property
    def supported_instrument_types(self) -> tuple[str, ...]:
        return self.feature_definition.supported_instrument_types

    def __post_init__(self) -> None:
        if self.ref.kind != "learning" or not re.fullmatch(r"[0-9a-f]{64}", self.implementation_hash):
            raise ValueError("Learning definition needs a learning ref and implementation hash")
        if not self.allowed_trading_refs or any(ref.kind != "trading" for ref in self.allowed_trading_refs):
            raise ValueError("Learning definition needs permitted trading refs")
        schema = freeze_json(validate_parameter_schema(thaw_json(self.parameter_schema)))
        object.__setattr__(self, "parameter_schema", schema)
        object.__setattr__(self, "allowed_trading_refs", tuple(self.allowed_trading_refs))
        object.__setattr__(self, "definition_hash", canonical_hash({
            "ref": self.ref.model_dump(mode="json"), "implementation": self.implementation_hash,
            "parameters": schema, "feature": self.feature_definition.definition_hash,
            "trading": [ref.model_dump(mode="json") for ref in self.allowed_trading_refs],
            "action": self.action_version, "reward": self.reward_version, "algorithm": self.algorithm,
            "hooks": [_hook_identity(hook) for hook in (self.feature_params_hook, self.requirements_hook,
                self.observation_encoder, self.action_decoder, self.reward_hook,
                self.schedule_hook, self.environment_factory)],
        }))

    def validate_config(self, config: PolicyTrainingSpec) -> Mapping[str, object]:
        if not isinstance(config, PolicyTrainingSpec):
            raise ValueError("Learning config must be a validated PolicyTrainingSpec")
        values = dict(lookback=config.lookback, frequency=config.frequency,
                      action_version=config.action_version, reward_version=config.reward_version)
        return freeze_json(validate_parameters(thaw_json(self.parameter_schema), values))

    def feature_params(self, config: PolicyTrainingSpec) -> Mapping[str, object]:
        self.validate_config(config)
        return freeze_json(validate_parameters(thaw_json(self.feature_definition.parameter_schema),
                                               thaw_json(self.feature_params_hook(config))))

    def resolve_requirements(self, params: Mapping[str, object], basket: FrozenBasketRevision,
                             interval: ResearchInterval) -> DataRequirements:
        effective = freeze_json(validate_parameters(thaw_json(self.parameter_schema), thaw_json(params)))
        return self.requirements_hook(effective, basket, interval)

    def encode_observation(self, env: DailyETFEnv):
        return self.observation_encoder(env)

    def decode_action(self, env: DailyETFEnv, action):
        return self.action_decoder(env, action)

    def reward(self, env: DailyETFEnv, interval: dict, equity: float) -> float:
        return self.reward_hook(env, interval, equity)

    def schedule(self, calendar, start, end, frequency):
        return self.schedule_hook(calendar, start, end, frequency)

    def make_environment(self, bundle, config, transform, **kwargs) -> gym.Env:
        return self.environment_factory(self, bundle, config, transform, **kwargs)


_ETF_LEARNING_IMPLEMENTATION_HASH = canonical_hash({
    "feature_params": inspect.getsource(_etf_feature_params),
    "requirements": inspect.getsource(_etf_learning_requirements),
    "encoder": inspect.getsource(_etf_encode), "decoder": inspect.getsource(_etf_decode),
    "reward": inspect.getsource(_etf_reward), "schedule": inspect.getsource(_etf_schedule),
    "factory": inspect.getsource(_etf_environment_factory),
    "environment": inspect.getsource(DailyETFEnv),
    "definition": inspect.getsource(LearningDefinition),
})


def _etf_definition(action: str, reward: str) -> LearningDefinition:
    action_name = "cash" if action == CASH_ACTION_VERSION else "legacy"
    reward_name = {LEGACY_REWARD_VERSION: "log", STATE_LOG_REWARD_VERSION: "state-log",
                   DSR_REWARD_VERSION: "dsr"}[reward]
    return LearningDefinition(
        ref=DefinitionRef(kind="learning", id=f"etf-ppo-{action_name}-{reward_name}", version="1"),
        implementation_hash=_ETF_LEARNING_IMPLEMENTATION_HASH,
        parameter_schema={"type": "object", "properties": {
            "lookback": {"type": "integer", "minimum": 3, "maximum": 512},
            "frequency": {"type": "string", "enum": ["weekly", "monthly"]},
            "action_version": {"type": "string", "const": action},
            "reward_version": {"type": "string", "const": reward},
        }, "required": ["lookback", "frequency", "action_version", "reward_version"],
          "additionalProperties": False},
        feature_definition=ETF_MARKET_FEATURES, allowed_trading_refs=(ETF_TRADING_DEFINITION.ref,),
        action_version=action, reward_version=reward, algorithm="sb3-ppo-v1",
        feature_params_hook=_etf_feature_params, requirements_hook=_etf_learning_requirements,
        observation_encoder=_etf_encode, action_decoder=_etf_decode, reward_hook=_etf_reward,
        schedule_hook=_etf_schedule, environment_factory=_etf_environment_factory,
    )


_ETF_DEFINITIONS = tuple(_etf_definition(action, reward)
                         for action in (LEGACY_ACTION_VERSION, CASH_ACTION_VERSION)
                         for reward in (LEGACY_REWARD_VERSION, STATE_LOG_REWARD_VERSION, DSR_REWARD_VERSION))


def etf_learning_definitions() -> tuple[LearningDefinition, ...]:
    return _ETF_DEFINITIONS


def create_environment(definition: LearningDefinition, bundle: DatasetBundle, config: PolicyTrainingSpec,
                       transform: FrozenTransform | None, *, backtest: BacktestSpec, training: bool,
                       asset_binding: AssetBinding, trading_definition: TradingDefinition,
                       table_semantics: tuple[TableSemantics, ...] | None = None,
                       cancelled: Callable[[], bool] = lambda: False, collect: bool = False) -> gym.Env:
    definition.validate_config(config)
    if trading_definition.ref not in definition.allowed_trading_refs:
        raise ValueError("DEFINITION_UNAVAILABLE: trading definition is not allowed by learning definition")
    if backtest.symbols != config.backtest.symbols:
        raise ValueError("MODEL_IDENTITY_MISMATCH: ordered learning basket differs from run basket")
    if set(backtest.symbols) - {item.local_symbol for item in asset_binding.assets}:
        raise ValueError("BINDING_MISMATCH: learning basket absent from asset binding")
    types = {item.instrument_id.instrument_type for item in asset_binding.assets if item.local_symbol in backtest.symbols}
    if not types.issubset(definition.feature_definition.supported_instrument_types):
        raise ValueError("DEFINITION_UNAVAILABLE: feature definition does not support learning basket")
    prices = next((item for item in asset_binding.table_semantics if item.table == "prices"), None)
    if prices is None or prices.temporal_rules.get("price_mode") != bundle.price_mode:
        raise ValueError("BINDING_MISMATCH: dataset price mode differs from frozen table semantics")
    if (definition.feature_definition.fit is None) != (transform is None):
        raise ValueError("MODEL_IDENTITY_MISMATCH: fitted transform requirement mismatch")
    return definition.make_environment(bundle, config, transform, backtest=backtest, training=training,
                                       asset_binding=asset_binding, trading_definition=trading_definition,
                                       table_semantics=table_semantics, cancelled=cancelled, collect=collect)
