from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.models import TABLE_COLUMNS
from quanto.config import BacktestSpec
from quanto.investing.contracts import LEGACY_ACTION_VERSION, PolicyBacktestSpec, PolicyTrainingSpec
from quanto.learning.artifacts import read_frozen_transform, read_manifest
from quanto.learning.contracts import check_cancelled
from quanto.learning.models import resolve_device
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.investing.rewards import LEGACY_REWARD_VERSION, reward_parameters
from quanto.reports.backtest_report import build_report

if TYPE_CHECKING:
    from quanto.factors.features import FrozenTransform
    from quanto.investing.feature_runtime import LearningRuntime


def policy_identity(config):
    from quanto.investing.environment import feature_version, environment_version, EXECUTION_VERSION

    return dict(feature_version=feature_version(config.lookback, config.reward_version),
                action_version=config.action_version, environment_version=environment_version(config.reward_version),
                execution_version=EXECUTION_VERSION, reward_version=config.reward_version,
                reward_parameters=reward_parameters(config.reward_version))


def check_policy_identity(manifest, config):
    expected = policy_identity(config)
    if any(manifest.get(name) != value for name, value in expected.items()) or manifest["basket"] != config.backtest.symbols:
        raise ValueError("policy observation, action, reward, execution version or basket identity mismatch")
    return expected


def evaluate_policy(model, bundle, spec, frequency="weekly", lookback=120,
                    cancelled=lambda: False, baseline="policy", emit=lambda event: None,
                    *, action_version=LEGACY_ACTION_VERSION, reward_version=LEGACY_REWARD_VERSION,
                    learning_runtime: LearningRuntime | None = None,
                    training_config: PolicyTrainingSpec | None = None,
                    feature_transform: FrozenTransform | None = None,
                    allow_foreign_transform: bool = False):
    from quanto.investing.environment import DailyETFEnv

    check_cancelled(cancelled)
    if learning_runtime is None:
        env = DailyETFEnv(bundle, spec, frequency=frequency, lookback=lookback,
                          training=False, action_version=action_version, reward_version=reward_version,
                          cancelled=cancelled, collect=True)
    else:
        if training_config is None or (frequency, lookback, action_version, reward_version) != (
                training_config.frequency, training_config.lookback,
                training_config.action_version, training_config.reward_version):
            raise ValueError("MODEL_IDENTITY_MISMATCH: evaluation settings differ from bound learning config")
        env = learning_runtime.create_environment(bundle, training_config, feature_transform,
                                                  backtest=spec, training=False,
                                                  cancelled=cancelled, collect=True,
                                                  allow_foreign_transform=allow_foreign_transform)
    try:
        observation, info = env.reset(seed=spec.seed)
        prefix_return = info["prefix_net_log_return"] if reward_version != LEGACY_REWARD_VERSION else 0.
        action_return = 0.
        rewards, steps = 0., 0
        finished = False
        while not finished:
            check_cancelled(cancelled)
            if baseline == "policy":
                action, _ = model.predict(observation, deterministic=True)
                observation, reward, terminated, truncated, info = env.step(action)
            elif baseline == "equal_weight" or baseline == "buy_hold" and steps == 0:
                weights = {symbol: 1. / len(spec.symbols) for symbol in spec.symbols}
                observation, reward, terminated, truncated, info = env.step_weights(weights)
            else:
                observation, reward, terminated, truncated, info = env.step(np.zeros(len(spec.symbols) + 1, dtype=np.int64))
            if not math.isfinite(float(reward)):
                raise ValueError("nonfinite policy evaluation reward")
            rewards += float(reward)
            action_return += info["step_net_log_return"]
            steps += 1
            finished = terminated or truncated
            emit(dict(kind="progress", message="policy evaluation", asof=info.get("asof", ""),
                      completed=steps, baseline=baseline))
        check_cancelled(cancelled)
        result = env.result()
        report = build_report(result, bundle, spec)
        net_log_return = math.log(float(report.metrics["final_equity"]) / spec.initial_cash)
        if not math.isclose(prefix_return + action_return, net_log_return, rel_tol=1e-7, abs_tol=1e-7):
            raise ValueError("policy net returns disagree with the shared daily account ledger")
        if reward_version == LEGACY_REWARD_VERSION and not math.isclose(rewards, net_log_return, rel_tol=1e-7, abs_tol=1e-7):
            raise ValueError("policy rewards disagree with the shared daily account ledger")
        metrics = dict(net_log_return=net_log_return, total_return=report.metrics["total_return"],
                       final_equity=report.metrics["final_equity"], max_drawdown=report.metrics["max_drawdown"],
                       sharpe=report.metrics["sharpe"], turnover=report.metrics["turnover"],
                       execution_cost=report.metrics["execution_cost"],
                       average_cash_ratio=float((result.equity.cash / result.equity.equity).mean()),
                       decision_steps=steps, observed_sessions=len(result.equity),
                       evaluation_reward_sum=rewards, prefix_net_log_return=prefix_return,
                       action_net_log_return=action_return,
                       **{key: value for key, value in env.reward_diagnostics.items() if key != "reward_sum"})
        return metrics, result, report
    finally:
        env.close()


def backtest_policy(policy_dir, dataset_dir, spec, output_dir, emit, cancelled, *,
                    learning_runtime: LearningRuntime | None = None, policy_context: dict | None = None):
    return _evaluate_saved_policy(policy_dir, dataset_dir, spec, output_dir, emit, cancelled,
                                  validation=False, learning_runtime=learning_runtime,
                                  policy_context=policy_context)


def evaluate_validation_policy(policy_dir, dataset_dir, output_dir, emit, cancelled, *,
                               learning_runtime: LearningRuntime | None = None):
    """Report the selected snapshot on its original model-selection data."""
    return _evaluate_saved_policy(policy_dir, dataset_dir, None, output_dir, emit, cancelled,
                                  validation=True, learning_runtime=learning_runtime,
                                  policy_context=None)


def _evaluate_saved_policy(policy_dir, dataset_dir, spec, output_dir, emit, cancelled, *, validation,
                           learning_runtime: LearningRuntime | None, policy_context: dict | None):
    from stable_baselines3 import PPO

    check_cancelled(cancelled)
    policy_dir, output_dir = Path(policy_dir), Path(output_dir)
    manifest = read_manifest(policy_dir, "policy")
    if policy_context is not None:
        from quanto.investing.policy_versions import verified_policy_manifest

        manifest = verified_policy_manifest(manifest, policy_context)
    elif learning_runtime is not None and not validation:
        raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy context missing")
    config = PolicyTrainingSpec.model_validate(manifest["config"])
    framework_learning = manifest.get("framework_learning")
    if (framework_learning is None) != (learning_runtime is None):
        raise ValueError("MODEL_IDENTITY_MISMATCH: framework model requires matching learning runtime")
    if validation:
        if digest(json_bytes(hash_files(Path(dataset_dir)))) != manifest["dataset_content_hash"]:
            raise ValueError("validation dataset content hash must match training dataset identity")
        spec = dict(start=config.validation_start, end=config.validation_end,
                    evaluation_purpose="model_selection")
    else:
        spec = spec.model_dump() if isinstance(spec, PolicyBacktestSpec) else dict(spec)
        if spec.get("evaluation_purpose") is None:
            spec["evaluation_purpose"] = "post_selection_backtest"
    request = PolicyBacktestSpec.model_validate(spec)
    if not validation:
        if request.evaluation_purpose == "model_selection":
            raise ValueError("evaluation_purpose model_selection is reserved for internal validation")
        purpose = ("development_replay" if request.start <= config.validation_end
                   else "post_selection_backtest")
        request = request.model_copy(update={"evaluation_purpose": purpose})
    expected = check_policy_identity(manifest, config)
    bundle = load_dataset(Path(dataset_dir))
    transform = None
    if learning_runtime is not None:
        evaluation_hash = digest(json_bytes(hash_files(Path(dataset_dir))))
        learning_runtime.validate_bundle(bundle, config, evaluation_hash)
        actual = learning_runtime.identity(config)
        if validation:
            if actual != framework_learning:
                raise ValueError("MODEL_IDENTITY_MISMATCH: validation framework learning identity differs")
        else:
            immutable = ("learning_ref", "learning_definition_hash", "learning_params",
                         "feature_ref", "feature_definition_hash", "feature_params",
                         "trading_ref", "trading_definition_hash", "ordered_assets")
            if any(actual[key] != framework_learning.get(key) for key in immutable):
                raise ValueError("MODEL_IDENTITY_MISMATCH: policy framework learning identity differs")
        descriptor = manifest.get("feature_transform")
        if learning_runtime.definition.feature_definition.fit is not None:
            if descriptor is None:
                raise ValueError("MODEL_IDENTITY_MISMATCH: fitted policy transform is missing")
            transform = read_frozen_transform(policy_dir, descriptor)
            # Backtests may use a different dataset; the transform provenance remains the training binding.
            if (transform.definition_ref != learning_runtime.definition.feature_definition.ref
                    or transform.definition_hash != learning_runtime.definition.feature_definition.definition_hash
                    or set(transform.training_dataset_hashes) != {framework_learning["dataset_content_hash"]}
                    or set(transform.training_binding_hashes) != {framework_learning["binding_hash"]}):
                raise ValueError("MODEL_IDENTITY_MISMATCH: policy transform provenance differs")
            if validation:
                learning_runtime.validate_transform(transform, config)
        elif descriptor is not None:
            raise ValueError("MODEL_IDENTITY_MISMATCH: fixed feature policy has unexpected transform")
    if "price_mode" in manifest and manifest["price_mode"] != bundle.price_mode:
        raise ValueError("policy price mode mismatch")
    values = config.backtest.model_dump(mode="json")
    values.update(start=request.start.isoformat(), end=request.end.isoformat())
    scalable_costs = (learning_runtime.trading.scalable_costs if learning_runtime is not None else
                      ("commission_rate", "minimum_commission", "slippage_rate"))
    for name in scalable_costs:
        values["execution"][name] *= request.cost_multiplier
    backtest = BacktestSpec.model_validate(values).validate_for_dataset(bundle)
    from dataclasses import replace
    from quanto.backtest.stock_trading import StockTradingDefinition
    if learning_runtime is not None and isinstance(learning_runtime.trading, StockTradingDefinition):
        learning_runtime = replace(learning_runtime, trading=learning_runtime.trading.with_cost_multiplier(request.cost_multiplier))
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("policy backtest output directory must be empty")
    if any(output_dir.resolve().is_relative_to(Path(source).resolve()) for source in (policy_dir, dataset_dir)):
        raise ValueError("report output directory must be outside model and dataset artifacts")
    model, policy_device = None, None
    if request.baseline == "policy":
        device = resolve_device(config.device)
        model = PPO.load(policy_dir / "policy.zip", device=device)
        if model.device.type != device:
            raise ValueError(f"policy inference device must be {device}; loader selected {model.device}")
        policy_device = str(model.device)
    metrics, result, report = evaluate_policy(model, bundle, backtest, config.frequency,
                                              config.lookback, cancelled, request.baseline, emit,
                                              action_version=config.action_version, reward_version=config.reward_version,
                                              learning_runtime=learning_runtime, training_config=config,
                                              feature_transform=transform, allow_foreign_transform=not validation)
    check_cancelled(cancelled)
    output_dir.mkdir(parents=True, exist_ok=True)
    daily = result.equity.copy()
    daily["drawdown"] = report.curves["drawdown"].to_numpy()
    frames = {name: getattr(result, name)[columns].copy() for name, columns in TABLE_COLUMNS.items()}
    frames.update(equity=daily, daily_equity=daily)
    for name, frame in frames.items():
        check_cancelled(cancelled)
        for column in frame.columns:
            frame[column] = frame[column].map(lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
                                             if isinstance(value, (dict, list)) else value)
        frame.to_csv(output_dir / f"{name}.csv", index=False)
    summary = dict(kind="policy_validation" if validation else "policy_backtest", version=1,
                   evaluation_purpose=request.evaluation_purpose,
                   policy_content_id=manifest["id"],
                   training_dataset_content_hash=manifest["dataset_content_hash"], baseline=request.baseline,
                   policy_device=policy_device,
                   frequency=config.frequency, cost_multiplier=request.cost_multiplier,
                   metrics={**report.metrics, **metrics}, metric_definitions={**report.metric_definitions,
                       "average_cash_ratio": {"description": "Mean actual daily cash divided by account equity; receivables remain separate",
                                              "period": "all observed sessions", "formula": "mean(daily_cash / daily_equity)"}},
                   period=dict(start=request.start.isoformat(), end=request.end.isoformat()),
                   counts={name: len(frame) for name, frame in frames.items()},
                   assumptions=dict(price_mode=bundle.price_mode, limitations=report.warnings,
                                    execution=backtest.execution.model_dump(mode="json"),
                                    reward=config.reward_version,
                                    reward_is_profit=config.reward_version == LEGACY_REWARD_VERSION,
                                    terminal_positions="marked to close; no forced liquidation"),
                   metadata=dict(config=config.model_dump(mode="json"), backtest=backtest.model_dump(mode="json"),
                                 source=bundle.manifest.source, limitations=bundle.manifest.limitations,
                                 policy_cutoff=manifest["cutoff"],
                                 validation_end=config.validation_end.isoformat(), **expected))
    check_cancelled(cancelled)
    (output_dir / "summary.json").write_bytes(json_bytes(summary))
    return summary
