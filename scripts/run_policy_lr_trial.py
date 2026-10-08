"""Standalone paired-learning-rate research; never publishes model versions."""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import shutil
import statistics

from quanto.backtest.dataset_loaders import load_dataset
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.environment import DailyETFEnv
from quanto.investing.evaluation import check_policy_identity, evaluate_policy
from quanto.investing.training import split_spec, validate_training_data
from quanto.learning.artifacts import read_manifest
from quanto.learning.models import resolve_device
from quanto.learning.registry import hash_files, runtime_versions
from quanto.objects import digest, file_hash, json_bytes


def load_trial_model(checkpoint, env, learning_rate, device):
    from stable_baselines3 import PPO
    from stable_baselines3.common.utils import FloatSchedule

    model = PPO.load(checkpoint, env=env, device=device)
    model.learning_rate = learning_rate
    model.lr_schedule = FloatSchedule(learning_rate)
    for group in model.policy.optimizer.param_groups:
        group["lr"] = learning_rate
    return model


def optimizer_step(model):
    return max((float(state["step"]) for state in model.policy.optimizer.state.values()), default=0.)


def run_trial(source, dataset, output, learning_rate, *, sessions=50, timesteps=512, device="cuda"):
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    import torch
    from stable_baselines3 import PPO

    source, dataset, output = Path(source).resolve(), Path(dataset).resolve(), Path(output).resolve()
    if source.is_file():
        if source.name != "last.zip":
            raise ValueError("source checkpoint must be last.zip")
        source = source.parent
    if output.exists() and any(output.iterdir()):
        raise ValueError("trial output directory must be empty")
    if output.is_relative_to(source) or output.is_relative_to(dataset):
        raise ValueError("trial output must be separate from source and dataset")
    if not math.isfinite(learning_rate) or not 0 < learning_rate <= 1 or sessions < 1 or timesteps < 1:
        raise ValueError("positive finite learning rate, sessions and timesteps required")
    parent = read_manifest(source, "policy")
    if "last.zip" not in parent["files"]:
        raise ValueError("source requires verified last.zip")
    config = PolicyTrainingSpec.model_validate(parent["config"])
    check_policy_identity(parent, config)
    dataset_hash = digest(json_bytes(hash_files(dataset)))
    if dataset_hash != parent["dataset_content_hash"]:
        raise ValueError("dataset content hash differs from source")
    bundle = load_dataset(dataset)
    if bundle.price_mode != parent["price_mode"]:
        raise ValueError("dataset price mode differs from source")
    validate_training_data(config, bundle)
    device = resolve_device(device)
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    checkpoint = source / "last.zip"
    initial = PPO.load(checkpoint, device=device)
    started = initial.num_timesteps
    if started != parent["timesteps_completed"]:
        raise ValueError("source last timesteps disagree with manifest")
    if initial.n_envs != 1 or timesteps % initial.n_steps:
        raise ValueError("session timesteps must be an exact multiple of the saved PPO rollout")

    def evaluate(model):
        metrics, result, _ = evaluate_policy(model, bundle, split_spec(config, "validation"),
                                             config.frequency, config.lookback,
                                             action_version=config.action_version, reward_version=config.reward_version)
        metrics.update(fill_count=len(result.fills), order_count=len(result.orders), no_trade=len(result.fills) == 0)
        return metrics

    initial_metrics = evaluate(initial)
    same_as_best = parent["files"]["last.zip"] == parent["files"]["policy.zip"]
    if same_as_best:
        for key in ("net_log_return", "total_return", "sharpe", "max_drawdown", "average_cash_ratio"):
            saved, actual = parent["metrics"]["validation"][key], initial_metrics[key]
            if saved != actual and (saved is None or actual is None or not math.isclose(saved, actual, rel_tol=1e-7, abs_tol=1e-7)):
                raise ValueError(f"initial validation differs from saved source metrics: {key}")
    del initial
    output.mkdir(parents=True, exist_ok=True)
    provenance = dict(source_path=str(source), source_content_id=parent["id"], source_last_hash=parent["files"]["last.zip"],
                      source_timesteps=started, dataset_content_hash=dataset_hash, learning_rate=learning_rate,
                      sessions=sessions, timesteps_per_session=timesteps, device=device, runtime=runtime_versions(),
                      config=config.model_dump(mode="json"), deterministic_algorithms=True)
    (output / "protocol.json").write_bytes(json_bytes(provenance))
    (output / "initial.json").write_bytes(json_bytes(dict(**provenance, metrics=initial_metrics,
        saved_validation_metrics=parent["metrics"]["validation"], matches_saved_validation=True if same_as_best else None)))
    best_metrics = initial_metrics if same_as_best else evaluate(PPO.load(source / "policy.zip", device=device))
    best_timesteps = parent["best_timesteps"]
    shutil.copyfile(source / "policy.zip", output / "policy.zip")
    metric = "sharpe" if config.selection_metric == "validation_sharpe" else "net_log_return"
    history = []
    for number in range(1, sessions + 1):
        env = DailyETFEnv(bundle, split_spec(config, "train"), frequency=config.frequency,
                          lookback=config.lookback, episode_sessions=config.episode_sessions, training=True,
                          action_version=config.action_version, reward_version=config.reward_version, collect=False)
        try:
            model = load_trial_model(checkpoint, env, learning_rate, device)
            session_start = model.num_timesteps
            seed = config.seed + session_start
            model.get_env().seed(seed)
            before_step = optimizer_step(model)
            rates_before = [group["lr"] for group in model.policy.optimizer.param_groups]
            model.learn(total_timesteps=timesteps, reset_num_timesteps=False)
            rates_after = [group["lr"] for group in model.policy.optimizer.param_groups]
            if model.num_timesteps != session_start + timesteps or any(rate != learning_rate for rate in rates_after):
                raise RuntimeError("PPO changed the requested session budget or actual learning rate")
            latest = evaluate(model)
            directory = output / f"session-{number:03d}"
            directory.mkdir()
            checkpoint = directory / "last.zip"
            model.save(checkpoint)
            score, incumbent = latest[metric], best_metrics[metric]
            if score is not None and math.isfinite(score) and (incumbent is None or score > incumbent):
                best_metrics, best_timesteps = latest, model.num_timesteps
                model.save(output / "policy.zip")
            shutil.copyfile(output / "policy.zip", directory / "policy.zip")
            scalars = {key: float(value) for key, value in model.logger.name_to_value.items()
                       if key.startswith("train/") and isinstance(value, (int, float)) and math.isfinite(value)}
            evidence = dict(**provenance, session=number, timesteps_started=session_start,
                timesteps_completed=model.num_timesteps, session_timesteps=timesteps,
                optimizer_step_before=before_step, optimizer_step_after=optimizer_step(model),
                learning_rates_before=rates_before, learning_rates_after=rates_after,
                sampling_provenance=dict(algorithm="base-seed-plus-parent-timesteps-v1", base_seed=config.seed,
                                         parent_timesteps=session_start, effective_env_seed=seed),
                latest_metrics=latest, best_metrics=best_metrics, best_timesteps=best_timesteps,
                training_sampling=env.training_sampling, training_sampling_intervals=env.training_sampling_intervals,
                training_diagnostics=env.reward_diagnostics, train_scalars=scalars,
                n_steps=model.n_steps, batch_size=model.batch_size, n_epochs=model.n_epochs,
                last_hash=file_hash(checkpoint), policy_hash=file_hash(directory / "policy.zip"))
            (directory / "session.json").write_bytes(json_bytes(evidence))
            history.append(evidence)
            print(f"session {number}/{sessions}: timesteps={model.num_timesteps}, sharpe={latest['sharpe']}, "
                  f"cash={latest['average_cash_ratio']:.6f}, fills={latest['fill_count']}", flush=True)
        finally:
            env.close()
    if read_manifest(source, "policy") != parent:
        raise RuntimeError("source artifact changed during trial")
    final_ten = history[-10:]
    sharpes = [entry["latest_metrics"]["sharpe"] for entry in final_ten if entry["latest_metrics"]["sharpe"] is not None]
    result = dict(**provenance, session_count=len(history), timesteps_started=started,
                  timesteps_completed=history[-1]["timesteps_completed"], best_metrics=best_metrics,
                  best_timesteps=best_timesteps, final_metrics=history[-1]["latest_metrics"],
                  final_ten_median_sharpe=statistics.median(sharpes) if sharpes else None,
                  final_ten_defined_sharpe_count=len(sharpes),
                  final_ten_no_trade_count=sum(entry["latest_metrics"]["no_trade"] for entry in final_ten),
                  last_checkpoint=str(checkpoint), history=history, source_verified_unchanged=True)
    (output / "result.json").write_bytes(json_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "dataset", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--learning-rate", type=float, required=True)
    parser.add_argument("--sessions", type=int, default=50)
    parser.add_argument("--timesteps", type=int, default=512)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    run_trial(args.source, args.dataset, args.output, args.learning_rate,
              sessions=args.sessions, timesteps=args.timesteps, device=args.device)


if __name__ == "__main__":
    main()
