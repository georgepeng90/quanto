from __future__ import annotations

from importlib.metadata import version
import math
import os
from pathlib import Path
import platform

import pandas as pd

from quanto.backtest.dataset_loaders import load_dataset
from quanto.config import BacktestSpec
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import evaluate_policy
from quanto.learning.artifacts import write_manifest
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
                          episode_sessions=config.episode_sessions, training=split == "train", collect=False)
        env.close()


def train_policy(spec, dataset_dir, output_dir, emit, cancelled, dataset_content_hash):
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import torch
    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import BaseCallback
    from quanto.investing.environment import DailyETFEnv, FEATURE_VERSION, ACTION_VERSION, ENVIRONMENT_VERSION, EXECUTION_VERSION

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
    last_evaluated_updates, next_evaluation = 0, config.eval_every_timesteps
    validation = split_spec(config, "validation")

    def select(model):
        nonlocal best_score, best_timesteps, best_metrics, last_evaluated_updates, next_evaluation
        check_cancelled(cancelled)
        metrics, _, _ = evaluate_policy(model, bundle, validation, config.frequency, config.lookback, cancelled)
        history.append(dict(timesteps=model.num_timesteps, updates=model._n_updates,
                            validation_net_log_return=metrics["net_log_return"]))
        if metrics["net_log_return"] > best_score:
            best_score, best_timesteps, best_metrics = metrics["net_log_return"], model.num_timesteps, metrics
            model.save(output_dir / "policy.zip")
        last_evaluated_updates = model._n_updates
        next_evaluation = model.num_timesteps + config.eval_every_timesteps
        emit(dict(kind="progress", message="policy validation after PPO update",
                  progress=min(model.num_timesteps / config.total_timesteps, 1.), **history[-1]))
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
                      training=True, cancelled=cancelled, collect=False)
    try:
        model = PPO("MlpPolicy", env, learning_rate=config.learning_rate, n_steps=config.n_steps,
                    batch_size=config.batch_size, n_epochs=config.n_epochs, gamma=config.gamma,
                    gae_lambda=config.gae_lambda, ent_coef=config.ent_coef,
                    policy_kwargs=dict(net_arch=dict(pi=[config.hidden_size, config.hidden_size],
                                                     vf=[config.hidden_size, config.hidden_size])),
                    seed=config.seed, device=device, verbose=0)
        emit(dict(kind="progress", message="PPO training started", progress=0., device=device))
        check_cancelled(cancelled)
        model.learn(total_timesteps=config.total_timesteps, callback=TrainingCallback())
        check_cancelled(cancelled)
        if model._n_updates > last_evaluated_updates:
            select(model)
        completed = model.num_timesteps
    except torch.cuda.OutOfMemoryError as exc:
        raise ValueError("CUDA memory exhausted; reduce PPO batch_size or hidden_size, or choose CPU") from exc
    finally:
        env.close()
    selected = PPO.load(output_dir / "policy.zip", device=device)
    test_metrics, _, _ = evaluate_policy(selected, bundle, split_spec(config, "test"),
                                         config.frequency, config.lookback, cancelled)
    check_cancelled(cancelled)
    (output_dir / "curves.json").write_bytes(json_bytes(history))
    runtime = dict(python=platform.python_version(), torch=torch.__version__,
                   stable_baselines3=version("stable-baselines3"), gymnasium=version("gymnasium"),
                   numpy=version("numpy"), cloudpickle=version("cloudpickle"), device=device,
                   cuda_version=torch.version.cuda, seed=config.seed, deterministic_algorithms=True,
                   device_name=torch.cuda.get_device_name() if device == "cuda" else "CPU")
    manifest = dict(kind="policy", version=1, name=config.name, config=config.model_dump(mode="json"),
                    basket=config.backtest.symbols, dataset_content_hash=dataset_content_hash,
                    cutoff=config.validation_end.isoformat(), feature_version=FEATURE_VERSION,
                    action_version=ACTION_VERSION, environment_version=ENVIRONMENT_VERSION,
                    execution_version=EXECUTION_VERSION, selection_metric="validation_net_log_return",
                    metrics=dict(validation=best_metrics, test=test_metrics), best_timesteps=best_timesteps,
                    timesteps_completed=completed, history=history, runtime=runtime,
                    limitations=bundle.manifest.limitations,
                    research_limits=["test evaluated once after checkpoint selection within this run",
                                     "no protection against repeated research on the test period",
                                     "save/load does not support exact training resume"])
    check_cancelled(cancelled)
    emit(dict(kind="progress", message="policy training evaluated", progress=1., metrics=manifest["metrics"]))
    check_cancelled(cancelled)
    return write_manifest(output_dir, manifest)
