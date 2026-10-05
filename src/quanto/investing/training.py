from __future__ import annotations

from importlib.metadata import version
import math
import os
from pathlib import Path
import platform
import shutil

import pandas as pd

from quanto.backtest.dataset_loaders import load_dataset
from quanto.config import BacktestSpec
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import evaluate_policy
from quanto.learning.artifacts import read_manifest, write_manifest
from quanto.learning.contracts import check_cancelled
from quanto.learning.models import resolve_device
from quanto.objects import json_bytes


def split_spec(config, split):
    values = config.backtest.model_dump(mode="json")
    values.update(start=getattr(config, f"{split}_start").isoformat(),
                  end=getattr(config, f"{split}_end").isoformat())
    return BacktestSpec.model_validate(values)


def validate_training_data(config, bundle):
    from quanto.investing.environment import DailyETFEnv, decision_dates

    config.backtest.validate_for_dataset(bundle)
    for split in ("train", "validation", "test"):
        spec = split_spec(config, split)
        decisions = decision_dates(bundle.calendar, spec.start, spec.end, config.frequency)
        if not len(decisions):
            raise ValueError(f"{split} partition has no executable {config.frequency} decision")
        history = bundle.prices[pd.to_datetime(bundle.prices.date) <= decisions[0]]
        counts = history.groupby("symbol").size()
        if not any(counts.get(symbol, 0) >= config.lookback for symbol in spec.symbols):
            raise ValueError(f"{split} partition has insufficient lookback history for all basket symbols")
        env = DailyETFEnv(bundle, spec, frequency=config.frequency, lookback=config.lookback,
                          episode_sessions=config.episode_sessions, training=split == "train",
                          action_version=config.action_version, collect=False)
        env.close()


def train_policy(spec, dataset_dir, output_dir, emit, cancelled, dataset_content_hash, *,
                 resume_dir=None, model_id=None, evaluate_test=True):
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import torch
    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import BaseCallback
    from quanto.investing.environment import DailyETFEnv, feature_version, ENVIRONMENT_VERSION, EXECUTION_VERSION

    config = PolicyTrainingSpec.model_validate(spec)
    check_cancelled(cancelled)
    bundle = load_dataset(Path(dataset_dir))
    validate_training_data(config, bundle)
    device = resolve_device(config.device)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("policy output directory must be empty")
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    history, best_score, best_timesteps, best_metrics = [], -math.inf, 0, None
    parent, best_source_content_id = None, None
    metric_name = "sharpe" if config.selection_metric == "validation_sharpe" else "net_log_return"
    expected = dict(feature_version=feature_version(config.lookback), action_version=config.action_version,
                    environment_version=ENVIRONMENT_VERSION, execution_version=EXECUTION_VERSION)
    if resume_dir is not None:
        resume_dir = Path(resume_dir)
        parent = read_manifest(resume_dir, "policy")
        if "last.zip" not in parent["files"]:
            raise ValueError("resume requires a verified last.zip training state")
        previous = PolicyTrainingSpec.model_validate(parent["config"])
        fixed = config.model_dump(mode="json", exclude={"total_timesteps", "device"})
        if fixed != previous.model_dump(mode="json", exclude={"total_timesteps", "device"}):
            raise ValueError("resume configuration must match the fixed model identity")
        if (parent["dataset_content_hash"] != dataset_content_hash or parent.get("model_id") != model_id
                or parent["basket"] != config.backtest.symbols or parent.get("price_mode") != bundle.price_mode
                or any(parent.get(name) != value for name, value in expected.items())):
            raise ValueError("resume dataset, model, basket or environment identity mismatch")
        history = list(parent["history"])
        best_timesteps, best_metrics = parent["best_timesteps"], parent["metrics"]["validation"]
        score = best_metrics[metric_name]
        if score is not None and math.isfinite(score):
            best_score = score
        best_source_content_id = parent.get("best_source_content_id") or parent["id"]
        shutil.copyfile(resume_dir / "policy.zip", output_dir / "policy.zip")
    validation = split_spec(config, "validation")

    def select(model):
        nonlocal best_score, best_timesteps, best_metrics, best_source_content_id, last_evaluated_updates, next_evaluation
        check_cancelled(cancelled)
        metrics, _, _ = evaluate_policy(model, bundle, validation, config.frequency, config.lookback,
                                        cancelled, action_version=config.action_version)
        if not math.isfinite(metrics["net_log_return"]):
            raise ValueError("nonfinite validation net log return")
        if metrics["sharpe"] is not None and not math.isfinite(metrics["sharpe"]):
            metrics["sharpe"] = None
        score = metrics[metric_name]
        valid = score is not None
        history.append(dict(timesteps=model.num_timesteps, updates=model._n_updates,
                            validation_net_log_return=metrics["net_log_return"], validation_sharpe=metrics["sharpe"],
                            validation_total_return=metrics["total_return"], validation_max_drawdown=metrics["max_drawdown"],
                            selection_valid=valid))
        if best_metrics is None or valid and score > best_score:
            best_score = score if valid else -math.inf
            best_timesteps, best_metrics, best_source_content_id = model.num_timesteps, metrics, None
            model.save(output_dir / "policy.zip")
        last_evaluated_updates = model._n_updates
        next_evaluation = model.num_timesteps + config.eval_every_timesteps
        emit(dict(kind="progress", message="policy validation after PPO update",
                  progress=min((model.num_timesteps - started) / config.total_timesteps, 1.), **history[-1]))
        check_cancelled(cancelled)

    class TrainingCallback(BaseCallback):
        def _on_step(self):
            check_cancelled(cancelled)
            return True

        def _on_rollout_start(self):
            # SB3 rollout_end precedes gradient updates. The next rollout_start follows them.
            if self.model._n_updates > last_evaluated_updates and self.model.num_timesteps >= next_evaluation:
                select(self.model)

    env = DailyETFEnv(bundle, split_spec(config, "train"), frequency=config.frequency,
                      lookback=config.lookback, episode_sessions=config.episode_sessions,
                      training=True, action_version=config.action_version, cancelled=cancelled, collect=False)
    try:
        if parent is not None:
            model = PPO.load(resume_dir / "last.zip", env=env, device=device)
            if model.num_timesteps != parent["timesteps_completed"]:
                raise ValueError("resume last state timesteps disagree with its manifest")
        else:
            model = PPO("MlpPolicy", env, learning_rate=config.learning_rate, n_steps=config.n_steps,
                        batch_size=config.batch_size, n_epochs=config.n_epochs, gamma=config.gamma,
                        gae_lambda=config.gae_lambda, ent_coef=config.ent_coef,
                        policy_kwargs=dict(net_arch=dict(pi=[config.hidden_size, config.hidden_size],
                                                         vf=[config.hidden_size, config.hidden_size])),
                        seed=config.seed, device=device, verbose=0)
        started = model.num_timesteps
        # PPO.load queues the original seed again. Override only the window sampler
        # after loading so continued sessions do not replay the same episode starts.
        effective_env_seed = config.seed + started
        model.get_env().seed(effective_env_seed)
        sampling_provenance = dict(algorithm="base-seed-plus-parent-timesteps-v1", base_seed=config.seed,
                                   parent_timesteps=started, effective_env_seed=effective_env_seed)
        last_evaluated_updates, next_evaluation = model._n_updates, started + config.eval_every_timesteps
        emit(dict(kind="progress", message="PPO training started", progress=0., device=device))
        check_cancelled(cancelled)
        model.learn(total_timesteps=config.total_timesteps, callback=TrainingCallback(), reset_num_timesteps=False)
        check_cancelled(cancelled)
        if model._n_updates > last_evaluated_updates:
            select(model)
        completed = model.num_timesteps
        model.save(output_dir / "last.zip")
    except torch.cuda.OutOfMemoryError as exc:
        raise ValueError("CUDA memory exhausted; reduce PPO batch_size or hidden_size, or choose CPU") from exc
    finally:
        env.close()
    test_metrics = None
    if evaluate_test:
        selected = PPO.load(output_dir / "policy.zip", device=device)
        test_metrics, _, _ = evaluate_policy(selected, bundle, split_spec(config, "test"),
                                             config.frequency, config.lookback, cancelled,
                                             action_version=config.action_version)
    check_cancelled(cancelled)
    (output_dir / "curves.json").write_bytes(json_bytes(history))
    runtime = dict(python=platform.python_version(), torch=torch.__version__,
                   stable_baselines3=version("stable-baselines3"), gymnasium=version("gymnasium"),
                   numpy=version("numpy"), cloudpickle=version("cloudpickle"), device=device,
                   cuda_version=torch.version.cuda, seed=config.seed, deterministic_algorithms=True,
                   device_name=torch.cuda.get_device_name() if device == "cuda" else "CPU")
    manifest = dict(kind="policy", version=1, name=config.name, config=config.model_dump(mode="json"),
                    basket=config.backtest.symbols, dataset_content_hash=dataset_content_hash, price_mode=bundle.price_mode,
                    model_id=model_id, parent_content_id=parent["id"] if parent else None,
                    cutoff=config.validation_end.isoformat(), feature_version=expected["feature_version"],
                    action_version=config.action_version, environment_version=ENVIRONMENT_VERSION,
                    execution_version=EXECUTION_VERSION, selection_metric=config.selection_metric,
                    selection_valid=math.isfinite(best_score), best_source_content_id=best_source_content_id,
                    metrics=dict(validation=best_metrics, test=test_metrics), best_timesteps=best_timesteps,
                    timesteps_started=started, timesteps_completed=completed, session_timesteps=completed - started,
                    history=history, runtime=runtime, sampling_provenance=sampling_provenance,
                    limitations=bundle.manifest.limitations,
                    research_limits=(["test evaluated once after checkpoint selection within this run"] if evaluate_test else []) + [
                                     "no protection against repeated research on the test period",
                                     "continuation resets account and reseeds only the window sampler from base seed plus parent timesteps; not exact uninterrupted replay"])
    check_cancelled(cancelled)
    emit(dict(kind="progress", message="policy training evaluated", progress=1., metrics=manifest["metrics"]))
    check_cancelled(cancelled)
    return write_manifest(output_dir, manifest)
