"""Prepare and compare a paired, recent-window policy reward trial.

This script writes model creation specs only. It never starts PPO training.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from quanto.backtest.dataset_loaders import load_dataset
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.environment import decision_dates
from quanto.investing.evaluation import check_policy_identity, evaluate_validation_policy
from quanto.investing.training import validate_training_data
from quanto.learning.artifacts import read_manifest
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes


CONTROL_REWARD = "net-log-return-with-state-v1"
DSR_REWARD = "differential-sharpe-v1"
VALIDATION_METRICS = ("sharpe", "net_log_return", "total_return", "max_drawdown",
                      "turnover", "execution_cost", "average_cash_ratio")


def _write_json(path: Path, value: dict) -> None:
    path.write_bytes(json_bytes(value) + b"\n")


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON file must contain an object: {path}")
    return value


def _output_is_available(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("output directory must be empty or not exist")


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _completed_cutoff(bundle, symbols: list[str]) -> pd.Timestamp:
    manifest = bundle.manifest
    start = max(pd.Timestamp(manifest.data_start), pd.Timestamp(manifest.calendar_start))
    end = min(pd.Timestamp(manifest.data_end), pd.Timestamp(manifest.calendar_end))
    calendar = pd.DatetimeIndex(pd.to_datetime(bundle.calendar["date"]))
    sessions = calendar[(calendar >= start) & (calendar <= end)]
    prices = bundle.prices
    status = bundle.status
    quoted = {symbol: set(pd.DatetimeIndex(pd.to_datetime(prices.loc[prices["symbol"] == symbol, "date"])))
              for symbol in symbols}
    suspended = {symbol: set(pd.DatetimeIndex(pd.to_datetime(status.loc[
        (status["symbol"] == symbol) & (status["status"] == "suspended"), "date"]))) for symbol in symbols}
    for day in reversed(sessions):
        if all(day in quoted[symbol] or day in suspended[symbol] for symbol in symbols):
            return pd.Timestamp(day)
    raise ValueError("dataset has no common completed basket session")


def _calendar_years_before(cutoff: pd.Timestamp, years: int) -> pd.Timestamp:
    # DateOffset clamps Feb 29 to Feb 28 for a non-leap anniversary.
    anniversary = cutoff - pd.DateOffset(years=years)
    return pd.Timestamp(anniversary.date() + pd.Timedelta(days=1))


def _check_interval_coverage(bundle, config: PolicyTrainingSpec, start: pd.Timestamp,
                             end: pd.Timestamp, label: str) -> None:
    calendar = pd.DatetimeIndex(pd.to_datetime(bundle.calendar["date"]))
    if start < pd.Timestamp(bundle.manifest.data_start) or end > pd.Timestamp(bundle.manifest.data_end):
        raise ValueError(f"{label} interval is outside dataset coverage")
    decisions = decision_dates(calendar, start.date(), end.date(), config.frequency)
    if not len(decisions):
        raise ValueError(f"{label} interval has no executable decision")
    first_decision = decisions[0]
    prices = bundle.prices
    for symbol in config.backtest.symbols:
        count = ((prices["symbol"] == symbol) & (pd.to_datetime(prices["date"]) <= first_decision)).sum()
        if count < config.lookback:
            raise ValueError(f"{label} interval lacks {config.lookback} lookback prices for {symbol}")


def prepare_trial(spec_path, dataset_path, output_dir, years=2):
    """Validate inputs and freeze paired control/DSR specs for one recent period."""
    spec_path, dataset_path, output_dir = map(Path, (spec_path, dataset_path, output_dir))
    if type(years) is not int or years not in (1, 2):
        raise ValueError("years must be 1 or 2")
    _output_is_available(output_dir)
    dataset_path, output_dir = dataset_path.resolve(), output_dir.resolve()
    if _inside(output_dir, dataset_path):
        raise ValueError("output directory cannot be inside the source dataset")

    source = PolicyTrainingSpec.model_validate(_load_json(spec_path))
    bundle = load_dataset(dataset_path)
    content_hash = digest(json_bytes(hash_files(dataset_path)))
    cutoff = _completed_cutoff(bundle, source.backtest.symbols)
    validation_start = _calendar_years_before(cutoff, years)
    validation_end = cutoff
    train_end = validation_start - pd.Timedelta(days=1)
    if pd.Timestamp(source.train_start) >= validation_start or pd.Timestamp(source.train_start) > train_end:
        raise ValueError("preserved train_start must precede the recent validation interval")
    if source.backtest.start > source.train_start or pd.Timestamp(source.backtest.start) < pd.Timestamp(bundle.manifest.data_start):
        raise ValueError("source backtest coverage does not include train_start")
    if validation_start < pd.Timestamp(bundle.manifest.data_start):
        raise ValueError("dataset does not cover the full requested recent validation interval")

    specs = {}
    for role, reward in (("control", CONTROL_REWARD), ("dsr", DSR_REWARD)):
        values = source.model_dump(mode="json")
        values.update(name=f"{source.name[:108]} [{role}]", reward_version=reward,
                      gamma=1,
                      train_end=train_end.date().isoformat(),
                      validation_start=validation_start.date().isoformat(),
                      validation_end=validation_end.date().isoformat(),
                      test_start=None, test_end=None, selection_metric="validation_sharpe")
        values["backtest"]["end"] = validation_end.date().isoformat()
        values["backtest"]["metrics"]["risk_free_rate"] = 0
        specs[role] = PolicyTrainingSpec.model_validate(values).model_dump(mode="json")
        config = PolicyTrainingSpec.model_validate(specs[role])
        validate_training_data(config, bundle)
        _check_interval_coverage(bundle, config, pd.Timestamp(config.train_start),
                                 pd.Timestamp(config.train_end), "train")
        _check_interval_coverage(bundle, config, validation_start, validation_end, "validation")

    trial = dict(schema_version=1, years=years, cutoff=cutoff.date().isoformat(),
                 train_start=source.train_start.isoformat(), train_end=train_end.date().isoformat(),
                 validation_start=validation_start.date().isoformat(), validation_end=validation_end.date().isoformat(),
                 dataset_path=str(dataset_path), dataset_content_hash=content_hash,
                 source_spec_path=str(spec_path.resolve()), specs=specs,
                 constraint_overrides=dict(gamma=dict(source=source.gamma, trial=1.),
                                           risk_free_rate=dict(source=source.backtest.metrics.risk_free_rate, trial=0.)),
                 comparison=dict(selection_metric="validation_sharpe", test_period=None,
                                 control_reward=CONTROL_REWARD, dsr_reward=DSR_REWARD))
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "trial.json", trial)
    _write_json(output_dir / "control-spec.json", specs["control"])
    _write_json(output_dir / "dsr-spec.json", specs["dsr"])
    return trial


def _validate_policy_pair(trial, control_policy_dir: Path, dsr_policy_dir: Path):
    specs = trial.get("specs")
    if not isinstance(specs, dict) or set(specs) != {"control", "dsr"}:
        raise ValueError("trial must contain control and dsr specs")
    manifests = {}
    expected_rewards = {"control": CONTROL_REWARD, "dsr": DSR_REWARD}
    for role, directory in (("control", control_policy_dir), ("dsr", dsr_policy_dir)):
        manifest = read_manifest(directory, "policy")
        config = PolicyTrainingSpec.model_validate(specs[role])
        manifest_config = manifest.get("config")
        if not isinstance(manifest_config, dict):
            raise ValueError(f"{role} policy is missing its training config")
        fixed_manifest_config = dict(manifest_config)
        fixed_trial_config = config.model_dump(mode="json")
        for values in (fixed_manifest_config, fixed_trial_config):
            values.pop("total_timesteps", None)
            values.pop("device", None)
        if fixed_manifest_config != fixed_trial_config:
            raise ValueError(f"{role} policy config differs from the prepared trial spec")
        if manifest.get("reward_version") != expected_rewards[role]:
            raise ValueError(f"{role} policy reward version differs from the prepared trial")
        if manifest.get("dataset_content_hash") != trial.get("dataset_content_hash"):
            raise ValueError(f"{role} policy dataset identity differs from the prepared trial")
        if "policy.zip" not in manifest.get("files", {}):
            raise ValueError(f"{role} policy artifact is missing policy.zip")
        check_policy_identity(manifest, config)
        if manifest.get("selection_metric") != "validation_sharpe":
            raise ValueError(f"{role} policy selection metric is not validation Sharpe")
        manifests[role] = manifest

    baseline = manifests["control"]
    candidate = manifests["dsr"]
    left_config = PolicyTrainingSpec.model_validate(specs["control"]).model_dump(mode="json")
    right_config = PolicyTrainingSpec.model_validate(specs["dsr"]).model_dump(mode="json")
    for values in (left_config, right_config):
        values.pop("name")
        values.pop("reward_version")
    if left_config != right_config:
        raise ValueError("control and DSR specs differ beyond name and reward version")
    for field in ("timesteps_started", "timesteps_completed", "session_timesteps"):
        if type(baseline.get(field)) is not int or baseline[field] != candidate.get(field):
            raise ValueError(f"policy {field} must be present and equal")
    if baseline["timesteps_started"] + baseline["session_timesteps"] != baseline["timesteps_completed"]:
        raise ValueError("control policy timestep history is inconsistent")
    if candidate["timesteps_started"] + candidate["session_timesteps"] != candidate["timesteps_completed"]:
        raise ValueError("DSR policy timestep history is inconsistent")
    left_history, right_history = baseline.get("history"), candidate.get("history")
    if left_history is not None or right_history is not None:
        if not isinstance(left_history, list) or not isinstance(right_history, list):
            raise ValueError("both policy validation histories must be available")
        left_steps = [item.get("timesteps") for item in left_history]
        right_steps = [item.get("timesteps") for item in right_history]
        if left_steps != right_steps:
            raise ValueError("policy validation histories use different training budgets")
    for field in ("feature_version", "environment_version", "execution_version", "action_version", "price_mode", "basket"):
        if baseline.get(field) != candidate.get(field):
            raise ValueError(f"policy {field} differs across rewards")
    left_runtime, right_runtime = baseline.get("runtime"), candidate.get("runtime")
    if left_runtime is not None or right_runtime is not None:
        if not isinstance(left_runtime, dict) or not isinstance(right_runtime, dict):
            raise ValueError("both policy runtime identities must be available")
        if left_runtime.get("device") != right_runtime.get("device"):
            raise ValueError("policy runtime devices differ across rewards")
    for role, directory in (("control", control_policy_dir), ("dsr", dsr_policy_dir)):
        if trial["dataset_path"] and not Path(trial["dataset_path"]).is_dir():
            raise ValueError("trial dataset directory is unavailable")
        config = PolicyTrainingSpec.model_validate(specs[role])
        if (config.validation_start.isoformat() != trial.get("validation_start")
                or config.validation_end.isoformat() != trial.get("validation_end")):
            raise ValueError("trial validation dates differ from prepared specs")
    return manifests


def _selection(manifest):
    metrics = manifest.get("metrics")
    validation = metrics.get("validation") if isinstance(metrics, dict) else None
    sharpe = validation.get("sharpe") if isinstance(validation, dict) else None
    valid = manifest.get("selection_valid") is True and isinstance(sharpe, (int, float)) and math.isfinite(sharpe)
    return valid, float(sharpe) if valid else None


def compare_trial(trial_path, control_policy_dir, dsr_policy_dir, output_dir):
    """Verify matching selected policy artifacts, then export paired validation reports."""
    trial_path, control_policy_dir, dsr_policy_dir, output_dir = map(
        Path, (trial_path, control_policy_dir, dsr_policy_dir, output_dir))
    trial = _load_json(trial_path)
    _output_is_available(output_dir)
    dataset_path = Path(trial["dataset_path"]).resolve()
    output_resolved = output_dir.resolve()
    for source in (dataset_path, control_policy_dir.resolve(), dsr_policy_dir.resolve()):
        if _inside(output_resolved, source):
            raise ValueError("comparison output directory must be outside dataset and model artifacts")
    manifests = _validate_policy_pair(trial, control_policy_dir, dsr_policy_dir)
    validations = {role: _selection(manifest) for role, manifest in manifests.items()}

    output_dir.parent.resolve().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="policy-reward-trial-") as temporary:
        scratch = Path(temporary)
        reports = {}
        for role, policy_dir in (("control", control_policy_dir), ("dsr", dsr_policy_dir)):
            summary = evaluate_validation_policy(policy_dir, dataset_path, scratch / role,
                                                 lambda event: None, lambda: False)
            if summary.get("kind") != "policy_validation" or summary.get("evaluation_purpose") != "model_selection":
                raise ValueError(f"{role} evaluator did not return a model-selection validation report")
            reports[role] = summary

        for role, summary in reports.items():
            valid, selected_sharpe = validations[role]
            evaluated_sharpe = summary.get("metrics", {}).get("sharpe")
            if valid and (not isinstance(evaluated_sharpe, (int, float)) or not math.isfinite(evaluated_sharpe)):
                validations[role] = (False, None)
            elif valid and not math.isclose(selected_sharpe, float(evaluated_sharpe), rel_tol=1e-7, abs_tol=1e-7):
                raise ValueError(f"{role} selected validation Sharpe does not match policy.zip evaluation")

        valid_scores = {role: score for role, (valid, score) in validations.items() if valid}
        winner = None
        if len(valid_scores) == 2 and valid_scores["control"] != valid_scores["dsr"]:
            winner = max(valid_scores, key=valid_scores.get)
        rows = {}
        for role in ("control", "dsr"):
            metrics = reports[role].get("metrics", {})
            rows[role] = {name: metrics.get(name) for name in VALIDATION_METRICS}
            rows[role]["evaluation_diagnostics"] = {name: metrics.get(name) for name in
                ("evaluation_reward_sum", "reward_daily_count", "reward_variance_floor_count", "reward_dsr_max_abs")}
            rows[role]["training_diagnostics"] = manifests[role].get("training_diagnostics")
            rows[role].update(selection_valid=validations[role][0], selection_sharpe=validations[role][1],
                              policy_content_id=manifests[role]["id"], model_id=manifests[role].get("model_id"),
                              timesteps_completed=manifests[role]["timesteps_completed"],
                              session_timesteps=manifests[role]["session_timesteps"])
        comparison = dict(schema_version=1, kind="policy_reward_comparison",
                          evaluation_purpose="model_selection", validation_purpose="model_selection",
                          validation_period=dict(start=trial["validation_start"], end=trial["validation_end"]),
                          dataset_content_hash=trial["dataset_content_hash"],
                          training=dict(timesteps_completed=manifests["control"]["timesteps_completed"],
                                        session_timesteps=manifests["control"]["session_timesteps"]),
                          results=rows, winner=winner,
                          reward_diagnostic_note="Training and evaluation reward sums are diagnostics for their own reward formula and must not be compared across control and DSR.",
                          limitations=["The current policy manifest does not encode every prior session boundary; verify full session histories and retain failed or cancelled session records before interpreting this pair."],
                          winner_rule="strictly higher finite selected validation Sharpe; no winner for invalid or tied scores")
        _write_json(scratch / "comparison.json", comparison)
        output_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(scratch / "control"), output_dir / "control")
        shutil.move(str(scratch / "dsr"), output_dir / "dsr")
        shutil.move(str(scratch / "comparison.json"), output_dir / "comparison.json")
    return comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="freeze paired specs for one recent validation period")
    prepare.add_argument("spec_path", type=Path)
    prepare.add_argument("dataset_path", type=Path)
    prepare.add_argument("output_dir", type=Path)
    prepare.add_argument("--years", type=int, choices=(1, 2), default=2)
    compare = commands.add_parser("compare", help="verify two policies and export validation reports")
    compare.add_argument("trial_path", type=Path)
    compare.add_argument("control_policy_dir", type=Path)
    compare.add_argument("dsr_policy_dir", type=Path)
    compare.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare_trial(args.spec_path, args.dataset_path, args.output_dir, args.years)
    else:
        result = compare_trial(args.trial_path, args.control_policy_dir, args.dsr_policy_dir, args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
