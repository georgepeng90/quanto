"""Replay latest PPO weights with fixed and sampled actions, without training."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from stable_baselines3 import PPO
from stable_baselines3.common.utils import set_random_seed

from quanto.backtest.dataset_loaders import load_dataset
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.environment import decision_dates
from quanto.investing.evaluation import check_policy_identity, evaluate_policy
from quanto.investing.training import split_spec
from quanto.learning.artifacts import read_manifest
from quanto.learning.registry import hash_files, runtime_versions
from quanto.objects import digest, file_hash, json_bytes


def trace_metrics(model, bundle, config, *, split="validation", deterministic=True, seed=None):
    """Use the shared account evaluator and record decisions before execution."""
    if not deterministic and seed is None:
        raise ValueError("stochastic diagnosis requires an explicit seed")
    if seed is not None:
        set_random_seed(seed, using_cuda=model.device.type == "cuda")
    spec = split_spec(config, split)
    dates = decision_dates(bundle.calendar, spec.start, spec.end, config.frequency)
    trace = []

    class Probe:
        def predict(self, observation, deterministic=True):
            with torch.no_grad():
                tensor, _ = model.policy.obs_to_tensor(observation)
                distribution = model.policy.get_distribution(tensor)
                probabilities = distribution.distribution[0].probs[0].cpu().tolist()
            action, state = model.predict(observation, deterministic=action_is_deterministic)
            trace.append(dict(date=dates[len(trace)].date().isoformat(), action=action.tolist(),
                              hold_probability=probabilities[0],
                              cash_ratio=float(observation[12 * len(spec.symbols)])))
            return action, state

    action_is_deterministic = deterministic
    metrics, result, _ = evaluate_policy(Probe(), bundle, spec, config.frequency, config.lookback,
        action_version=config.action_version, reward_version=config.reward_version)
    buys = result.fills[result.fills.side == "buy"]
    first_entry = None if buys.empty else buys.trade_date.min().date().isoformat()
    before_entry = [row["hold_probability"] for row in trace
                    if first_entry is None or row["date"] < first_entry]
    return dict(**metrics, deterministic=deterministic, seed=seed, fill_count=len(result.fills),
                first_entry_date=first_entry, hold_count=sum(row["action"][0] == 0 for row in trace),
                pre_entry_hold_probability_mean=float(np.mean(before_entry)) if before_entry else None,
                trace=trace)


def summarize_runs(runs):
    result = dict(count=len(runs), no_trade_count=sum(run["fill_count"] == 0 for run in runs), metrics={})
    for key in ("sharpe", "total_return", "max_drawdown", "average_cash_ratio", "fill_count",
                "hold_count", "pre_entry_hold_probability_mean"):
        values = [run[key] for run in runs if run.get(key) is not None]
        quantiles = np.quantile(values, [.5, .1, .9]).tolist() if values else [None] * 3
        result["metrics"][key] = dict(defined_count=len(values), median=quantiles[0],
                                      p10=quantiles[1], p90=quantiles[2])
    return result


def diagnose(source, dataset, output, *, label, device="cpu", repeats=20, seed_start=1000, baselines=False):
    source, dataset, output = (Path(path).resolve() for path in (source, dataset, output))
    if repeats < 1:
        raise ValueError("repeats must be positive")
    if any(output.is_relative_to(path) or path.is_relative_to(output) for path in (source, dataset)):
        raise ValueError("diagnostic output must be separate from source artifacts")
    if output.exists() and any(output.iterdir()):
        raise ValueError("diagnostic output must be empty")
    manifest = read_manifest(source, "policy")
    config = PolicyTrainingSpec.model_validate(manifest["config"])
    check_policy_identity(manifest, config)
    dataset_hash = digest(json_bytes(hash_files(dataset)))
    if dataset_hash != manifest["dataset_content_hash"]:
        raise ValueError("diagnosis dataset identity mismatch")
    bundle = load_dataset(dataset)
    if bundle.price_mode != manifest["price_mode"]:
        raise ValueError("diagnosis price mode mismatch")
    checkpoint = source / "last.zip"
    checkpoint_hash = file_hash(checkpoint)
    model = PPO.load(checkpoint, device=device)
    if model.num_timesteps != manifest["timesteps_completed"]:
        raise ValueError("checkpoint timesteps disagree with source manifest")
    output.mkdir(parents=True, exist_ok=True)
    metadata = dict(label=label, source=str(source), source_content_id=manifest["id"],
                    checkpoint="last.zip", checkpoint_hash=checkpoint_hash, dataset_hash=dataset_hash,
                    steps=model.num_timesteps, device=str(model.device), runtime=runtime_versions(),
                    stochastic_seeds=list(range(seed_start, seed_start + repeats)), config=manifest["config"])
    (output / "protocol.json").write_bytes(json_bytes(metadata))
    fixed = trace_metrics(model, bundle, config)
    (output / "deterministic.json").write_bytes(json_bytes(fixed))
    expected = manifest["history"][-1]
    for key in ("total_return", "max_drawdown", "sharpe"):
        actual, original = fixed[key], expected[f"validation_{key}"]
        if (actual is None) != (original is None) or (actual is not None and not np.isclose(actual, original, atol=1e-8, rtol=1e-7)):
            raise ValueError(f"deterministic replay disagrees with saved latest {key}")
    sampled = []
    for seed in metadata["stochastic_seeds"]:
        run = trace_metrics(model, bundle, config, deterministic=False, seed=seed)
        (output / f"seed-{seed}.json").write_bytes(json_bytes(run))
        sampled.append(run)
        print(json.dumps(dict(label=label, completed=len(sampled), total=repeats,
                              seed=seed, total_return=run["total_return"], cash=run["average_cash_ratio"])), flush=True)
    baseline_metrics = {}
    if baselines:
        for baseline in ("equal_weight", "cash"):
            metrics, result, _ = evaluate_policy(None, bundle, split_spec(config, "validation"),
                config.frequency, config.lookback, baseline=baseline,
                action_version=config.action_version, reward_version=config.reward_version)
            baseline_metrics[baseline] = dict(**metrics, fill_count=len(result.fills))
    if file_hash(checkpoint) != checkpoint_hash or read_manifest(source, "policy")["id"] != manifest["id"]:
        raise ValueError("source artifact changed during diagnosis")
    summary = dict(protocol=metadata, deterministic={k: v for k, v in fixed.items() if k != "trace"},
                   stochastic=summarize_runs(sampled), baselines=baseline_metrics)
    (output / "summary.json").write_bytes(json_bytes(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--seed-start", type=int, default=1000)
    parser.add_argument("--baselines", action="store_true")
    torch.set_num_threads(1)
    diagnose(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
