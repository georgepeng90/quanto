"""Prepare registered-model action experiments and compare exported validation manifests.

This script never submits jobs. Run the generated create-model/train-model requests
through the existing investing CLI or API, then export each policy detail as
<case-id>.json for the aggregate command. No test-period result is used.
Exported manifests record runtime identity but cannot attest the worker's Git revision.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

import pandas as pd

from quanto.backtest.dataset_loaders import load_dataset
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.training import validate_training_data
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes


ACTIONS = ("hold-target-quartiles-v1", "hold-target-cash-quartiles-v2")
VALIDATION_FIELDS = ("sharpe", "net_log_return", "max_drawdown", "average_cash_ratio",
                     "average_invested_ratio", "turnover", "execution_cost")
REQUIRED_VALIDATION = ("sharpe", "net_log_return", "max_drawdown")
RUNTIME_FIELDS = ("python", "torch", "stable_baselines3", "gymnasium", "numpy", "cloudpickle",
                  "device", "cuda_version", "device_name", "deterministic_algorithms")
IDENTITY_FIELDS = ("feature_version", "environment_version", "execution_version", "price_mode")


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _distinct_integers(values, label, minimum):
    if not values or any(type(value) is not int or value < minimum for value in values) or len(set(values)) != len(values):
        raise ValueError(f"{label} must contain distinct integers >= {minimum}")
    return tuple(values)


def prepare_matrix(protocol_path, dataset_id, dataset_content_hash, output_dir, *,
                   budget=4096, horizons=(126, 252, 504), seeds=(17, 42, 83)):
    """Validate every case before writing dry-run requests; require registered frozen data."""
    protocol_path, output_dir = Path(protocol_path), Path(output_dir)
    horizons = _distinct_integers(horizons, "horizons", 2)
    seeds = _distinct_integers(seeds, "seeds", 0)
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("dataset_id must identify a registered dataset")
    if not isinstance(dataset_content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", dataset_content_hash):
        raise ValueError("dataset_content_hash must be the registered 64-character digest")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output directory must be empty")

    protocol = json.loads(protocol_path.read_text(encoding="utf-8-sig"))
    dataset_path = Path(protocol["dataset_path"])
    if not dataset_path.is_absolute():
        dataset_path = protocol_path.parent / dataset_path
    dataset_path = dataset_path.resolve()
    if output_dir.resolve().is_relative_to(dataset_path):
        raise ValueError("output directory cannot be inside the registered dataset")
    actual_hash = digest(json_bytes(hash_files(dataset_path)))
    if actual_hash != dataset_content_hash:
        raise ValueError("dataset_content_hash does not match protocol dataset_path; use the registered frozen directory")
    bundle = load_dataset(dataset_path)
    original = PolicyTrainingSpec.model_validate(protocol["spec"])
    if type(budget) is not int or budget < original.n_steps or budget % original.n_steps:
        raise ValueError("budget must be a positive multiple of n_steps with at least one full PPO rollout")
    train_sessions = int(((pd.to_datetime(bundle.calendar.date) >= pd.Timestamp(original.train_start)) &
                          (pd.to_datetime(bundle.calendar.date) <= pd.Timestamp(original.train_end))).sum())

    for horizon in horizons:
        if horizon > train_sessions:
            raise ValueError(f"episode_sessions={horizon} exceeds the training interval of {train_sessions} sessions")
        candidate = original.model_copy(update={"episode_sessions": horizon, "total_timesteps": budget})
        validate_training_data(candidate, bundle)

    base = original.model_dump(mode="json")
    cases = []
    for action_index, action in enumerate(ACTIONS, 1):
        for horizon in horizons:
            for seed in seeds:
                case_id = f"v{action_index}-h{horizon}-s{seed}"
                values = {**base, "name": f"{original.name[:80]} | {case_id}",
                          "action_version": action, "episode_sessions": horizon,
                          "total_timesteps": budget, "seed": seed,
                          "backtest": {**base["backtest"], "seed": seed}}
                spec = PolicyTrainingSpec.model_validate(values).model_dump(mode="json")
                cases.append({"id": case_id, "action_version": action, "episode_sessions": horizon,
                              "seed": seed, "spec": spec})

    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
                              capture_output=True, text=True, check=False)
    matrix = {"schema_version": 1, "dataset_id": dataset_id, "dataset_content_hash": dataset_content_hash,
              "dataset_path": str(dataset_path), "protocol_sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
              "preparation_git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
              "train_sessions": train_sessions, "budget": budget, "n_steps": original.n_steps,
              "window_start_positions": {str(horizon): train_sessions - horizon + 1 for horizon in horizons},
              "cases": cases}
    output_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        folder = output_dir / case["id"]
        folder.mkdir()
        _write_json(folder / "create-model.json", {"dataset_id": dataset_id, "spec": case["spec"]})
        _write_json(folder / "train-model.json", {"additional_timesteps": budget,
                                                   "device": case["spec"]["device"]})
    _write_json(output_dir / "matrix.json", matrix)
    return matrix


def aggregate_matrix(matrix_path, manifests_dir, output_path):
    """Read exported registered policy details, reject missing or contaminated cases."""
    matrix = json.loads(Path(matrix_path).read_text(encoding="utf-8"))
    expected = {case["id"]: case for case in matrix["cases"]}
    if len(expected) != len(matrix["cases"]):
        raise ValueError("duplicate case in matrix")
    exports = {}
    for path in sorted(Path(manifests_dir).glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        case_id = document.get("case_id", path.stem)
        if case_id in exports:
            raise ValueError(f"duplicate export for {case_id}")
        if case_id not in expected:
            raise ValueError(f"unexpected case export: {case_id}")
        exports[case_id] = document.get("manifest", document)
    if set(exports) != set(expected):
        raise ValueError(f"missing case exports: {sorted(set(expected) - set(exports))}")

    results = []
    shared_identity = None
    for case in matrix["cases"]:
        manifest = exports[case["id"]]
        if manifest.get("kind") != "policy" or not manifest.get("model_id"):
            raise ValueError(f"{case['id']}: expected a registered persistent-model policy")
        if manifest.get("config") != case["spec"]:
            raise ValueError(f"{case['id']}: config differs from frozen matrix")
        if manifest.get("action_version") != case["action_version"]:
            raise ValueError(f"{case['id']}: action_version differs from frozen matrix")
        if manifest.get("dataset_content_hash") != matrix["dataset_content_hash"]:
            raise ValueError(f"{case['id']}: dataset content hash differs from frozen matrix")
        runtime = manifest.get("runtime")
        if (any(name not in manifest for name in IDENTITY_FIELDS) or not isinstance(runtime, dict)
                or any(name not in runtime for name in RUNTIME_FIELDS)):
            raise ValueError(f"{case['id']}: missing environment, price mode, or runtime identity")
        if runtime["device"] != case["spec"]["device"]:
            raise ValueError(f"{case['id']}: runtime device differs from requested device")
        identity = {name: manifest[name] for name in IDENTITY_FIELDS}
        identity["runtime"] = {name: runtime[name] for name in RUNTIME_FIELDS}
        if shared_identity is None:
            shared_identity = identity
        elif identity != shared_identity:
            raise ValueError(f"{case['id']}: environment, price mode, or runtime differs across cases")
        if (manifest.get("timesteps_started") != 0 or manifest.get("session_timesteps") != matrix["budget"]
                or manifest.get("timesteps_completed") != matrix["budget"]):
            raise ValueError(f"{case['id']}: completed timesteps differ from equal budget")
        metrics = manifest.get("metrics", {})
        if "test" not in metrics or metrics["test"] is not None:
            raise ValueError(f"{case['id']}: test metrics were evaluated during training")
        validation = metrics.get("validation")
        if not isinstance(validation, dict) or any(name not in validation for name in REQUIRED_VALIDATION):
            raise ValueError(f"{case['id']}: missing required validation metrics")
        reported = {name: validation[name] for name in VALIDATION_FIELDS if name in validation}
        results.append({"id": case["id"], "action_version": case["action_version"],
                        "episode_sessions": case["episode_sessions"], "seed": case["seed"],
                        "model_id": manifest["model_id"], "policy_content_id": manifest.get("id"),
                        "selection_valid": manifest.get("selection_valid"), "validation": reported})

    by_key = {(row["action_version"], row["episode_sessions"], row["seed"]): row for row in results}
    pairs = []
    for horizon in sorted({case["episode_sessions"] for case in matrix["cases"]}):
        for seed in sorted({case["seed"] for case in matrix["cases"]}):
            old, new = (by_key[(action, horizon, seed)] for action in ACTIONS)
            delta = {name: new["validation"][name] - old["validation"][name]
                     for name in VALIDATION_FIELDS if name in old["validation"] and name in new["validation"]
                     and isinstance(old["validation"][name], (int, float))
                     and isinstance(new["validation"][name], (int, float))
                     and math.isfinite(old["validation"][name]) and math.isfinite(new["validation"][name])}
            pairs.append({"episode_sessions": horizon, "seed": seed, "v1": old["id"], "v2": new["id"],
                          "delta_v2_minus_v1": delta})
    summary = {"dataset_id": matrix["dataset_id"], "dataset_content_hash": matrix["dataset_content_hash"],
               "preparation_git_revision": matrix.get("preparation_git_revision"),
               "budget": matrix["budget"], "train_sessions": matrix["train_sessions"],
               "window_start_positions": matrix["window_start_positions"], "recorded_runtime_identity": shared_identity,
               "cases": results, "pairs": pairs}
    _write_json(Path(output_path), summary)
    return summary


def _integers(raw):
    return tuple(int(value) for value in raw.split(","))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--protocol", type=Path, required=True)
    prepare.add_argument("--dataset-id", required=True)
    prepare.add_argument("--dataset-content-hash", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--budget", type=int, default=4096)
    prepare.add_argument("--horizons", type=_integers, default=(126, 252, 504))
    prepare.add_argument("--seeds", type=_integers, default=(17, 42, 83))
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--matrix", type=Path, required=True)
    aggregate.add_argument("--manifests", type=Path, required=True)
    aggregate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        prepare_matrix(args.protocol, args.dataset_id, args.dataset_content_hash, args.output,
                       budget=args.budget, horizons=args.horizons, seeds=args.seeds)
    else:
        aggregate_matrix(args.matrix, args.manifests, args.output)


if __name__ == "__main__":
    main()
