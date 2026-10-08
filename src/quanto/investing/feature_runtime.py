"""Per-run learning references and train-only feature transform lifecycle."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from quanto.backtest.trading import TradingDefinition
from quanto.data.point_in_time import TableSemantics, _bind_point_in_time, session_cutoff
from quanto.factors.features import FrozenTransform, compute_fit_frame, fit_transform
from quanto.framework import AssetBinding, canonical_hash, thaw_json
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.definitions import LearningDefinition, create_environment

if TYPE_CHECKING:
    from quanto.backtest.dataset_schemas import DatasetBundle
    from quanto.config import BacktestSpec


def _tables(bundle: DatasetBundle) -> dict:
    return {**{name: getattr(bundle, name) for name in
               ("prices", "benchmark", "calendar", "instruments", "status", "events")},
            **bundle.auxiliary}


@dataclass(frozen=True)
class LearningRuntime:
    definition: LearningDefinition
    binding: AssetBinding
    trading: TradingDefinition
    table_semantics: tuple[TableSemantics, ...] | None = None

    def identity(self, config: PolicyTrainingSpec) -> dict:
        learning_params = self.definition.validate_config(config)
        feature_params = self.definition.feature_params(config)
        by_symbol = {asset.local_symbol: asset for asset in self.binding.assets}
        try:
            ordered = tuple(by_symbol[symbol] for symbol in config.backtest.symbols)
        except KeyError as exc:
            raise ValueError("BINDING_MISMATCH: ordered basket is absent from asset binding") from exc
        return {
            "learning_ref": self.definition.ref.model_dump(mode="json"),
            "learning_definition_hash": self.definition.definition_hash,
            "learning_params": thaw_json(learning_params),
            "feature_ref": self.definition.feature_definition.ref.model_dump(mode="json"),
            "feature_definition_hash": self.definition.feature_definition.definition_hash,
            "feature_params": thaw_json(feature_params),
            "trading_ref": self.trading.ref.model_dump(mode="json"),
            "trading_definition_hash": self.trading.definition_hash,
            "dataset_version_id": self.binding.dataset_version_id,
            "dataset_content_hash": self.binding.dataset_content_hash,
            "binding_hash": self.binding.binding_hash,
            "ordered_assets": [{"local_symbol": asset.local_symbol,
                                "instrument_id": asset.instrument_id.model_dump(mode="json")}
                               for asset in ordered],
        }

    def validate_bundle(self, bundle: DatasetBundle, config: PolicyTrainingSpec,
                        dataset_content_hash: str) -> None:
        self.identity(config)
        if dataset_content_hash != self.binding.dataset_content_hash:
            raise ValueError("BINDING_MISMATCH: dataset content hash differs from learning binding")
        self.binding.validate_symbols(tuple(bundle.instruments.symbol))
        if self.trading.ref not in self.definition.allowed_trading_refs:
            raise ValueError("DEFINITION_UNAVAILABLE: learning definition does not permit trading definition")
        selected = {item.local_symbol: item for item in self.binding.assets}
        types = {selected[symbol].instrument_id.instrument_type for symbol in config.backtest.symbols}
        if not types.issubset(self.definition.feature_definition.supported_instrument_types):
            raise ValueError("DEFINITION_UNAVAILABLE: feature does not support basket instrument types")
        if not types.issubset(self.trading.supported_instrument_types) or bundle.price_mode not in self.trading.supported_price_modes:
            raise ValueError("DEFINITION_UNAVAILABLE: trading definition does not support bound data")
        prices = next((item for item in self.binding.table_semantics if item.table == "prices"), None)
        if prices is None or prices.temporal_rules.get("price_mode") != bundle.price_mode:
            raise ValueError("BINDING_MISMATCH: frozen price semantics differ from dataset")

    def validate_transform(self, transform: FrozenTransform, config: PolicyTrainingSpec, *,
                           require_training_binding: bool = True) -> None:
        feature = self.definition.feature_definition
        if (transform.definition_ref != feature.ref or transform.definition_hash != feature.definition_hash
                or transform.params_hash != canonical_hash(self.definition.feature_params(config))
                or transform.recipe != feature.transform_recipe):
            raise ValueError("MODEL_IDENTITY_MISMATCH: feature transform definition or parameters differ")
        if (require_training_binding and
                (set(transform.training_dataset_hashes) != {self.binding.dataset_content_hash}
                 or set(transform.training_binding_hashes) != {self.binding.binding_hash})):
            raise ValueError("MODEL_IDENTITY_MISMATCH: feature transform training binding differs")
        if not config.train_start <= transform.training_start <= transform.training_end <= config.train_end:
            raise ValueError("MODEL_IDENTITY_MISMATCH: feature transform training coverage differs")

    def fit_transform(self, bundle: DatasetBundle, config: PolicyTrainingSpec) -> FrozenTransform | None:
        feature = self.definition.feature_definition
        if feature.fit is None:
            return None
        self.validate_bundle(bundle, config, self.binding.dataset_content_hash)
        days = pd.DatetimeIndex(pd.to_datetime(bundle.calendar.date)).sort_values().unique()
        train_days = days[(days >= pd.Timestamp(config.train_start)) & (days <= pd.Timestamp(config.train_end))]
        if train_days.empty:
            raise ValueError("DATA_REQUIREMENT_UNSATISFIED: no training sessions for feature fit")
        symbols = tuple(config.backtest.symbols)
        tables = _tables(bundle)
        params = self.definition.feature_params(config)
        frames = tuple(compute_fit_frame(feature, _bind_point_in_time(
            tables, self.binding, symbols, session_cutoff(day.date()), semantics=self.table_semantics), params)
                       for day in train_days)
        transform = fit_transform(feature, frames, params)
        self.validate_transform(transform, config)
        return transform

    def create_environment(self, bundle: DatasetBundle, config: PolicyTrainingSpec,
                           transform: FrozenTransform | None, *, backtest: BacktestSpec,
                           training: bool, cancelled, collect: bool,
                           allow_foreign_transform: bool = False):
        if transform is not None:
            self.validate_transform(transform, config, require_training_binding=not allow_foreign_transform)
        return create_environment(self.definition, bundle, config, transform, backtest=backtest,
                                  training=training, asset_binding=self.binding,
                                  trading_definition=self.trading, table_semantics=self.table_semantics,
                                  cancelled=cancelled, collect=collect)
