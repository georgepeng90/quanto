from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.models import TABLE_COLUMNS
from quanto.config import BacktestSpec
from quanto.investing.contracts import LEGACY_ACTION_VERSION, PolicyBacktestSpec, PolicyTrainingSpec
from quanto.learning.artifacts import read_manifest
from quanto.learning.contracts import check_cancelled
from quanto.learning.models import resolve_device
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.investing.rewards import LEGACY_REWARD_VERSION, reward_parameters
from quanto.reports.backtest_report import build_report


def policy_identity(config):
    from quanto.investing.environment import feature_version, environment_version, EXECUTION_VERSION

    return dict(feature_version=feature_version(config.lookback, config.reward_version),
                action_version=config.action_version, environment_version=environment_version(config.reward_version),
                execution_version=EXECUTION_VERSION, reward_version=config.reward_version,
                reward_parameters=reward_parameters(config.reward_version))


def check_policy_identity(manifest, config):
    expected = policy_identity(config)
    # Old immutable artifacts predate explicit reward metadata.
    actual = {"reward_version": LEGACY_REWARD_VERSION, "reward_parameters": {}, **manifest}
    if any(actual.get(name) != value for name, value in expected.items()) or manifest["basket"] != config.backtest.symbols:
        raise ValueError("policy observation, action, reward, execution version or basket identity mismatch")
    return expected


def evaluate_policy(model, bundle, spec, frequency="weekly", lookback=120,
                    cancelled=lambda: False, baseline="policy", emit=lambda event: None,
                    *, action_version=LEGACY_ACTION_VERSION, reward_version=LEGACY_REWARD_VERSION):
    from quanto.investing.environment import DailyETFEnv

    check_cancelled(cancelled)
    env = DailyETFEnv(bundle, spec, frequency=frequency, lookback=lookback,
                      training=False, action_version=action_version, reward_version=reward_version,
                      cancelled=cancelled, collect=True)
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


def backtest_policy(policy_dir, dataset_dir, spec, output_dir, emit, cancelled):
    return _evaluate_saved_policy(policy_dir, dataset_dir, spec, output_dir, emit, cancelled, validation=False)


def evaluate_validation_policy(policy_dir, dataset_dir, output_dir, emit, cancelled):
    """Report the selected snapshot on its original model-selection data."""
    return _evaluate_saved_policy(policy_dir, dataset_dir, None, output_dir, emit, cancelled, validation=True)


def _evaluate_saved_policy(policy_dir, dataset_dir, spec, output_dir, emit, cancelled, *, validation):
    from stable_baselines3 import PPO

    check_cancelled(cancelled)
    policy_dir, output_dir = Path(policy_dir), Path(output_dir)
    manifest = read_manifest(policy_dir, "policy")
    config = PolicyTrainingSpec.model_validate(manifest["config"])
    if validation:
        if digest(json_bytes(hash_files(Path(dataset_dir)))) != manifest["dataset_content_hash"]:
            raise ValueError("validation dataset content hash must match training dataset identity")
        spec = dict(start=config.validation_start, end=config.validation_end)
    request = PolicyBacktestSpec.model_validate(spec)
    expected = check_policy_identity(manifest, config)
    bundle = load_dataset(Path(dataset_dir))
    if "price_mode" in manifest and manifest["price_mode"] != bundle.price_mode:
        raise ValueError("policy price mode mismatch")
    values = config.backtest.model_dump(mode="json")
    values.update(start=request.start.isoformat(), end=request.end.isoformat())
    for name in ("commission_rate", "minimum_commission", "slippage_rate"):
        values["execution"][name] *= request.cost_multiplier
    backtest = BacktestSpec.model_validate(values).validate_for_dataset(bundle)
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
                                              action_version=config.action_version, reward_version=config.reward_version)
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
                   evaluation_purpose="model_selection" if validation else "post_selection_backtest",
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
                                 policy_cutoff=manifest["cutoff"], **expected))
    check_cancelled(cancelled)
    (output_dir / "summary.json").write_bytes(json_bytes(summary))
    return summary
