"""Read-only final audit of the dedicated fixed-basket training run.

Example: python scripts/report_fixed_basket_training.py --state PATH/state.json
         --protocol docs/research/2026-10-05-fixed-basket-training/protocol.json
         --output PATH/audit [--plot-python PYTHON_WITH_MATPLOTLIB]
No API writes, model loading, training, or checkpoint selection occur here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]

def monitoring_checkpoints(protocol):
    rounds = protocol["rounds"]
    start = protocol.get("monitor_start_round", 10)
    history = protocol.get("historical_monitor_rounds", [])
    require(type(rounds) is int and rounds >= 50 and type(start) is int
            and 0 < start <= rounds and (rounds - start) % 10 == 0, "invalid monitoring budget")
    require(all(type(n) is int and 0 < n < start for n in history)
            and history == sorted(set(history)), "invalid historical monitoring checkpoints")
    return tuple(history) + tuple(range(start, rounds + 1, 10))


def evaluation_keys(protocol, extension=False):
    keys = {f"monitor_{n:03d}_{snapshot}" for n in monitoring_checkpoints(protocol) for snapshot in ("best", "last")}
    keys |= {"monitor_equal_weight", "monitor_buy_hold"}
    prefix = f"final_{protocol['rounds']:03d}_" if extension else "final_"
    keys |= {prefix + snapshot for snapshot in ("best", "last", "equal_weight", "buy_hold")}
    if extension:
        keys |= {"historical_final_050_" + snapshot for snapshot in ("best", "last", "equal_weight", "buy_hold")}
    return keys


def evaluation_role(key, rounds):
    historical = key.startswith("historical_final_050_")
    period = "monitor" if key.startswith("monitor_") else "final"
    suffix = key.removeprefix("historical_final_050_" if historical else period + "_")
    number = int(suffix[:3]) if suffix[:3].isdigit() else (50 if historical else 10 if period == "monitor" else rounds)
    role = suffix[4:] if suffix[:3].isdigit() else suffix
    return period, number, role, historical


def verify_inherited_evaluation(key, entry, source, source_path):
    original_key = key
    if original_key not in source["evaluations"]:
        original_key = "final_" + key.removeprefix("historical_final_050_")
    original = source["evaluations"][original_key]
    subset(original, entry, "original evaluation")
    expected = Path(original.get("source_summary_path") or source_path.parent / (original_key + ".json"))
    require(Path(entry["source_summary_path"]).resolve() == expected.resolve(), "original evaluation source path changed")


def verify_session_device(protocol, extension, number, manifest):
    expected = dict(protocol["spec"])
    device = protocol["device_override"] if extension and number > 50 else expected["device"]
    expected["device"] = device
    subset(expected, manifest["config"])
    require(manifest["runtime"]["device"] == device, f"round {number}: runtime device mismatch")
    if extension:
        require(device == ("cpu" if number <= 50 else "cuda"), "CPU/CUDA session boundary changed")
        if number > 50:
            require(bool(manifest["runtime"].get("cuda_version")), "CUDA session lacks CUDA runtime evidence")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def subset(expected, actual, label="spec"):
    for key, value in expected.items():
        require(key in actual, f"{label}: missing {key}")
        if isinstance(value, dict):
            subset(value, actual[key], label + "." + key)
        else:
            require(actual[key] == value, f"{label}.{key}: changed")


def verified_directory(path, expected):
    root = Path(path)
    require(root.is_dir() and not root.is_symlink(), "artifact directory missing/symbolic")
    actual = {}
    for file in sorted(root.rglob("*")):
        require(not file.is_symlink(), "symbolic artifact member")
        if file.is_file():
            actual[file.relative_to(root).as_posix()] = sha(file)
    require(actual == expected, f"registered file set/hash mismatch: {root}")
    return actual


def checkpoint(path):
    # SB3 stores plain scalar training counters in data; policy.pth is the weight file.
    with zipfile.ZipFile(path) as archive:
        data = json.loads(archive.read("data"))
        weights = hashlib.sha256(archive.read("policy.pth")).hexdigest()
        optimizer = hashlib.sha256(archive.read("policy.optimizer.pth")).hexdigest()
    return {"path": str(Path(path).resolve()), "zip_sha256": sha(path), "weights_sha256": weights,
            "optimizer_sha256": optimizer, "num_timesteps": data["num_timesteps"],
            "ppo_n_updates": data.get("_n_updates")}


def equity_metrics(path, spec):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    values = [float(row["equity"]) for row in rows]
    require(values and all(math.isfinite(v) and v > 0 for v in values), "invalid equity values")
    initial = spec["initial_cash"]
    returns = [v / previous - 1 for v, previous in zip(values, [initial] + values[:-1])]
    sd = statistics.pstdev(returns) if len(returns) >= 2 else None
    annual = spec["metrics"]["annual_sessions"]
    risk_free = spec["metrics"]["risk_free_rate"]
    sharpe = ((statistics.mean(returns) - risk_free / annual) * math.sqrt(annual) / sd
              if sd is not None and sd > 0 else None)
    peak, drawdown = initial, 0.
    for value in values:
        peak = max(peak, value)
        drawdown = max(drawdown, 1 - value / peak)
    return {"total_return": values[-1] / initial - 1, "sharpe": sharpe,
            "max_drawdown": drawdown, "final_equity": values[-1], "observed_sessions": len(values)}, rows


def metrics_equal(expected, actual):
    for key, value in actual.items():
        other = expected.get(key)
        if value is None:
            require(other is None, f"undefined {key} was not preserved")
        else:
            require(other is not None and math.isclose(value, other, rel_tol=1e-8, abs_tol=1e-8),
                    f"equity disagrees with {key}")


def plot_monitoring(rows, path, actions_per_round=512):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(3, 1, figsize=(10, 10), sharex=True, constrained_layout=True)
    for axis, metric, label in zip(axes, ("total_return", "sharpe", "max_drawdown"),
                                   ("Net return (%)", "Daily-equity Sharpe", "Maximum drawdown (%)")):
        scale = 1 if metric == "sharpe" else 100
        for snapshot, title in (("validation_best", "Selected policy.zip"), ("last.zip_offline", "Latest parameters")):
            series = sorted((row for row in rows if row["snapshot"] == snapshot), key=lambda row: int(row["round"]))
            axis.plot([int(row["round"]) for row in series],
                      [math.nan if row[metric] is None else float(row[metric]) * scale for row in series],
                      marker="o", label=title)
        for baseline, style in (("equal_weight", "--"), ("buy_hold", ":")):
            row = next(row for row in rows if row["snapshot"] == baseline)
            if row[metric] is not None:
                axis.axhline(float(row[metric]) * scale, linestyle=style, label=baseline.replace("_", " "))
        axis.set_ylabel(label)
        axis.grid(alpha=.25)
        axis.legend(loc="best")
        policy_rows = [row for row in rows if row["snapshot"] in {"validation_best", "last.zip_offline"}]
        if metric == "sharpe" and all(row[metric] is None for row in policy_rows):
            axis.text(.5, .15, "Policy Sharpe undefined (zero variance)", ha="center", transform=axis.transAxes)
    axes[0].set_title("Fixed-basket PPO: 2025 monitoring period")
    axes[-1].set_xlabel(f"Successful continuation sessions ({actions_per_round} actions each)")
    axes[-1].set_xticks(sorted({int(row["round"]) for row in rows if row["snapshot"] in {"validation_best", "last.zip_offline"}}))
    figure.savefig(path, dpi=160)
    plt.close(figure)


class Audit:
    def __init__(self, state, protocol, workspace, state_path):
        self.state, self.protocol, self.state_path = state, protocol, Path(state_path).resolve()
        self.workspace = Path(workspace).resolve()
        self.db = sqlite3.connect((self.workspace / "workbench.sqlite3").as_uri() + "?mode=ro", uri=True)
        self.db.row_factory = sqlite3.Row

    def row(self, table, identity):
        require(table in {"jobs", "datasets", "learning_artifacts", "investing_models"}, "invalid table")
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        require(row is not None, f"missing registered {table}: {identity}")
        return dict(row)

    def registered(self, identity, kind):
        row = self.row("learning_artifacts", identity)
        require(row["kind"] == kind and self.row("jobs", identity)["state"] == "succeeded", "artifact not successful")
        files = verified_directory(row["path"], json.loads(row["files_json"]))
        require(hashlib.sha256(json_bytes(files)).hexdigest() == row["content_hash"], "registry content hash mismatch")
        name = "manifest.json" if kind == "policy" else "summary.json"
        manifest = read_json(Path(row["path"]) / name)
        require(manifest == json.loads(row["manifest_json"]), "file/registry manifest mismatch")
        if kind == "policy":
            require(hashlib.sha256(json_bytes({k: v for k, v in manifest.items() if k != "id"})).hexdigest() == manifest["id"],
                    "policy manifest content identity mismatch")
            require(manifest["files"] == {k: v for k, v in files.items() if k != "manifest.json"}, "policy member hashes mismatch")
        return manifest, {"id": identity, "kind": kind, "path": str(Path(row["path"]).resolve()),
                          "content_hash": row["content_hash"], "verified_file_sha256": files}

    def run(self):
        require(self.state.get("complete") is True, "not complete")
        rounds, budget = self.protocol["rounds"], self.protocol["timesteps_per_round"]
        checkpoints = monitoring_checkpoints(self.protocol)
        require(type(budget) is int and budget > 0, "invalid action budget")
        require(len(self.state["rounds"]) == rounds, "recorded round count disagrees with protocol")
        extension = self.state.get("extension")
        require(bool(extension) == bool(self.protocol.get("source_state_path")), "extension protocol/state mismatch")
        source_state = None
        if extension:
            source_path = Path(self.protocol["source_state_path"]).resolve()
            require(sha(source_path) == self.protocol["source_state_sha256"], "original state changed")
            source_state = read_json(source_path)
            base_rounds = len(source_state["rounds"])
            stop = source_state.get("stop", {})
            stopped = stop.get("successful_rounds") == base_rounds and stop.get("active_job_id", "unknown") is None
            require(base_rounds >= 50 and (source_state.get("complete") is True or stopped),
                    "extension requires a completed or explicitly stopped successful prefix")
            require(extension["source_state_sha256"] == self.protocol["source_state_sha256"]
                    and Path(extension["source_state_path"]).resolve() == source_path
                    and extension["base_rounds"] == base_rounds and extension["device_override"] == "cuda"
                    and self.protocol["device_override"] == "cuda", "extension provenance mismatch")
            require(self.state["rounds"][:base_rounds] == source_state["rounds"], "original training prefix changed")
            require(all(self.state[key] == source_state[key] == extension[key] for key in ("model_id", "dataset_id")),
                    "extension created a different model or dataset")
            require(extension["base_version_id"] == source_state["rounds"][-1]["version_id"]
                    and extension["base_content_id"] == source_state["rounds"][-1]["content_id"], "extension base changed")
            require(extension["source_protocol_hash"] == source_state["protocol_hash"], "original protocol identity changed")
        model = self.row("investing_models", self.state["model_id"])
        require(model["dataset_id"] == self.state["dataset_id"], "model dataset mismatch")
        spec = json.loads(model["spec_json"])
        subset(self.protocol["spec"], spec)
        require(budget % spec["n_steps"] == 0, "budget must contain complete PPO rollouts")
        monitor, final = self.protocol["monitor_period"], self.protocol["final_period"]
        require(spec["validation_end"] < monitor["start"] < monitor["end"] < final["start"] < final["end"],
                "monitoring/final cutoffs overlap validation or each other")
        require(final == {"start": spec["test_start"], "end": spec["test_end"]}, "final test partition changed")
        sessions = self.db.execute("SELECT s.*, j.state FROM investing_sessions s JOIN jobs j ON j.id=s.job_id "
                                   "WHERE model_id=? ORDER BY j.enqueue_seq", (model["id"],)).fetchall()
        # An immutable original audit can still verify its prefix after authorized continuation.
        require(len(sessions) >= rounds and all(s["state"] == "succeeded" for s in sessions[:rounds]),
                "model has insufficient successful ordered sessions")
        sessions = sessions[:rounds]
        dataset = self.row("datasets", self.state["dataset_id"])
        # Dataset content hash uses the same canonical map of actual member hashes.
        directory = Path(dataset["path"])
        expected = {f.relative_to(directory).as_posix(): sha(f) for f in directory.rglob("*") if f.is_file()}
        verified_directory(directory, expected)
        require(hashlib.sha256(json_bytes(expected)).hexdigest() == dataset["content_hash"], "dataset content changed")
        policies, manifests, duplicate_groups, records_by_content = [], {}, {}, {}
        prior_id = prior_content = None
        for number, (entry, session) in enumerate(zip(self.state["rounds"], sessions), 1):
            require(entry["round"] == number and entry["version_id"] == session["job_id"], "round order mismatch")
            require(session["base_version_id"] == prior_id, "session parent mismatch")
            manifest, record = self.registered(entry["version_id"], "policy")
            for key, value in {"model_id": model["id"], "parent_content_id": prior_content,
                               "dataset_content_hash": dataset["content_hash"], "timesteps_started": (number - 1) * budget,
                               "timesteps_completed": number * budget, "session_timesteps": budget}.items():
                require(manifest.get(key) == value, f"round {number}: {key} mismatch")
            require(manifest["metrics"]["test"] is None, "training evaluated reserved test")
            verify_session_device(self.protocol, extension, number, manifest)
            if self.protocol.get("require_sampling_algorithm") and number > extension["base_rounds"]:
                subset(dict(algorithm=self.protocol["require_sampling_algorithm"], base_seed=spec["seed"],
                            parent_timesteps=(number - 1) * budget,
                            effective_env_seed=spec["seed"] + (number - 1) * budget),
                       manifest.get("sampling_provenance", {}), "window sampling")
            require(entry["content_id"] == manifest["id"] and entry["validation_best"] == manifest["metrics"]["validation"],
                    "state/policy identity or validation metric mismatch")
            latest = {key.removeprefix("validation_"): value for key, value in manifest["history"][-1].items()
                      if key.startswith("validation_")}
            require(entry["validation_latest"] == latest, "latest validation metrics changed")
            record["round"] = number
            record["runtime_device"] = manifest["runtime"]["device"]
            record.update({key: manifest[key] for key in ("best_timesteps", "best_source_content_id", "selection_valid")})
            record["validation_best"] = manifest["metrics"]["validation"]
            record["validation_latest"] = latest
            record["last"] = checkpoint(Path(record["path"]) / "last.zip")
            record["best"] = checkpoint(Path(record["path"]) / "policy.zip")
            require(record["last"]["num_timesteps"] == number * budget, "last.zip timestep counter mismatch")
            expected_updates = number * (budget // spec["n_steps"]) * spec["n_epochs"]
            require(record["last"]["ppo_n_updates"] == expected_updates, "last.zip PPO epoch update counter mismatch")
            require(record["best"]["num_timesteps"] == manifest["best_timesteps"], "best.zip timestep counter mismatch")
            source_id = manifest["best_source_content_id"]
            if source_id is not None:
                require(source_id in records_by_content, "best snapshot source outside verified parent chain")
                require(record["best"]["zip_sha256"] == records_by_content[source_id]["best"]["zip_sha256"],
                        "inherited best snapshot bytes changed")
            duplicate_groups.setdefault(record["best"]["zip_sha256"], []).append(number)
            policies.append(record)
            manifests[entry["version_id"]] = manifest
            records_by_content[manifest["id"]] = record
            prior_id, prior_content = entry["version_id"], manifest["id"]
        evaluations, monitoring = {}, []
        expected_keys = evaluation_keys(self.protocol, bool(extension))
        require(set(self.state["evaluations"]) == expected_keys, "missing or unexpected evaluations")
        for key in sorted(expected_keys):
            entry = self.state["evaluations"][key]
            if extension and (key.startswith("historical_final_050_") or key in source_state["evaluations"]):
                verify_inherited_evaluation(key, entry, source_state, source_path)
            summary_path = Path(entry.get("source_summary_path") or self.state_path.parent / (key + ".json"))
            summary = read_json(summary_path)
            require(summary == entry["summary"], "state/local evaluation summary mismatch")
            period_name, expected_round, role, historical = evaluation_role(key, rounds)
            dates = self.protocol["monitor_period" if period_name == "monitor" else "final_period"]
            require(summary["period"] == dates and entry["period"] == period_name, "evaluation period mismatch")
            number = entry["round"]
            require(number in checkpoints and (period_name != "final" or number == (50 if historical else rounds)), "evaluation round mismatch")
            require(number == expected_round, "evaluation key/round mismatch")
            require(entry["version_id"] == self.state["rounds"][number - 1]["version_id"], "evaluation version mismatch")
            training = manifests[entry["version_id"]]
            is_last = key.endswith("_last")
            require(bool(entry.get("job_id")) != is_last, "registered/offline evaluation role mismatch")
            expected_snapshot = "last.zip_offline" if is_last else "validation_best" if key.endswith("_best") else role
            require(entry["snapshot"] == expected_snapshot, "snapshot role mismatch")
            if entry.get("job_id"):
                actual, record = self.registered(entry["job_id"], "policy_backtest")
                require(actual == summary and actual["model_id"] == model["id"], "registered backtest mismatch")
                require(actual["policy_id"] == entry["version_id"] and actual["dataset_id"] == dataset["id"], "backtest references mismatch")
                require(actual["policy_content_id"] == training["id"], "backtest policy content mismatch")
                expected_baseline = "policy" if key.endswith("_best") else role
                require(actual["baseline"] == expected_baseline and actual["cost_multiplier"] == 1, "baseline/cost mismatch")
                equity_path = Path(record["path"]) / "equity.csv"
            else:
                require(summary["kind"] == "offline_last_evaluation" and summary["registered_in_workbench"] is False,
                        "offline result mislabeled")
                require(summary["policy_content_id"] == training["id"] and summary["version_id"] == entry["version_id"],
                        "offline source mismatch")
                equity_path = Path(entry.get("source_equity_path") or self.state_path.parent / (key + ".equity.csv"))
                record = {"registered": False, "summary_path": str(summary_path), "summary_sha256": sha(summary_path),
                          "equity_path": str(equity_path), "equity_sha256": sha(equity_path),
                          "integrity_limit": "Offline files have no independently pinned publication hash."}
            if extension and period_name == "final" and not historical:
                require(not entry.get("source_summary_path") and not entry.get("source_equity_path"),
                        "new final evaluation reused original output paths")
                original_jobs = {e.get("job_id") for e in source_state["evaluations"].values() if e.get("job_id")}
                require(not entry.get("job_id") or entry["job_id"] not in original_jobs,
                        "new final evaluation reused an original backtest job")
            inference_device = self.protocol.get("execution_scope", {}).get("policy_inference_device")
            if inference_device and number >= extension["base_rounds"] and (is_last or role == "best"):
                actual_device = summary.get("runtime", {}).get("device") if is_last else summary.get("policy_device")
                require(str(actual_device).split(":")[0] == inference_device, "policy inference device mismatch")
            actual_metrics, equity_rows = equity_metrics(equity_path, spec["backtest"])
            metrics_equal(summary["metrics"], actual_metrics)
            require(all(dates["start"] <= r["date"][:10] <= dates["end"] for r in equity_rows), "equity outside period")
            record.update(version_id=entry["version_id"], round=number, period=period_name,
                          snapshot=entry["snapshot"], metrics=summary["metrics"])
            if extension and period_name == "final":
                record["evaluation_context"] = "original round-50 historical evaluation" if historical else "repeated historical evaluation after CUDA continuation; not untouched holdout"
            record["no_trades"] = summary["metrics"].get("fill_count") == 0
            record["all_cash"] = summary["metrics"].get("average_invested_ratio") == 0
            evaluations[key] = record
            if period_name == "monitor":
                monitoring.append({"evaluation": key, "round": number, "snapshot": entry["snapshot"],
                                   "version_id": entry["version_id"], "backtest_id": entry.get("job_id"),
                                   **{metric: summary["metrics"].get(metric) for metric in ("total_return", "sharpe", "max_drawdown", "fill_count", "average_invested_ratio")}})
        trend = {}
        for snapshot in ("validation_best", "last.zip_offline"):
            series = sorted((r for r in monitoring if r["snapshot"] == snapshot), key=lambda r: r["round"])
            deltas, improvements = {}, []
            for metric in ("total_return", "sharpe", "max_drawdown"):
                first, last = series[0][metric], series[-1][metric]
                delta = None if first is None or last is None else last - first
                deltas[metric] = delta
                if delta is not None:
                    improvements.append(-delta if metric == "max_drawdown" else delta)
            label = ("unchanged" if improvements and all(abs(v) < 1e-10 for v in improvements)
                     else "improved_on_all_available_metrics" if improvements and all(v >= 0 for v in improvements) and any(v > 0 for v in improvements)
                     else "deteriorated_on_all_available_metrics" if improvements and all(v <= 0 for v in improvements) and any(v < 0 for v in improvements)
                     else "mixed_or_undefined")
            trend[snapshot] = {f"round_{checkpoints[0]}_to_{checkpoints[-1]}_deltas": deltas, "description": label}
        scores = [record["validation_best"]["sharpe"] for record in policies if record["selection_valid"]]
        require(all(score is not None for score in scores), "valid selection has undefined score")
        require(all(b >= a for a, b in zip(scores, scores[1:])), "validation-selected score decreased")
        result = {"audit_passed": True, "successful_rounds": rounds, "timesteps": rounds * budget,
                  "extension": extension,
                  "monitoring_checkpoint_counts": {"registered_policy": len(checkpoints), "offline_last": len(checkpoints)},
                  "model": {"id": model["id"], "dataset_id": dataset["id"], "spec": spec,
                            "sqlite_path": str(self.workspace / "workbench.sqlite3"),
                            "ui_url": "http://127.0.0.1:8770/models/" + model["id"]},
                  "dataset": {"id": dataset["id"], "path": str(directory.resolve()),
                              "content_hash": dataset["content_hash"], "verified_file_sha256": expected},
                  "state_path": str(self.state_path), "state_sha256": sha(self.state_path),
                  "policies": policies, "evaluations": evaluations, "monitoring_trend": trend,
                  "policy_zip_identical_groups": {h: rounds for h, rounds in duplicate_groups.items() if len(rounds) > 1},
                  "training_evidence": {"first_to_last_latest_weights_changed": policies[0]["last"]["weights_sha256"] != policies[-1]["last"]["weights_sha256"],
                                        "first_to_last_latest_optimizer_changed": policies[0]["last"]["optimizer_sha256"] != policies[-1]["last"]["optimizer_sha256"],
                                        "final_latest_num_timesteps": policies[-1]["last"]["num_timesteps"],
                                        "final_latest_ppo_epoch_updates": policies[-1]["last"]["ppo_n_updates"],
                                        "expected_gradient_batch_steps": rounds * (budget // spec["n_steps"]) * spec["n_epochs"] * (spec["n_steps"] // spec["batch_size"]),
                                        "optimizer_step_tensors_loaded": False},
                  "valid_validation_selection": manifests[prior_id]["selection_valid"],
                  "validation_selected_score_non_decreasing": True if scores else None,
                  "interpretation": ["Validation-selected best is a running maximum by construction; its validation monotonicity is not out-of-sample improvement.",
                                     "Monitoring compares the same historical 2025 period; endpoint changes are descriptive, not statistical evidence of generalization.",
                                     "Undefined Sharpe remains null. No fills and all-cash exposure are reported explicitly.",
                                     "Identical policy.zip hashes mean byte-identical selected snapshots, even when version IDs differ.",
                                     ("2026 is a repeated historical evaluation after the original round-50 evaluation; it is no longer an untouched holdout."
                                      if extension else "Final test was not used by training; this audit cannot certify absence of human research leakage.")]}
        return result, monitoring


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--protocol", type=Path, default=ROOT / "docs/research/2026-10-05-fixed-basket-training/protocol.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--plot-python", type=Path)
    parser.add_argument("--plot-csv", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--plot-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--plot-actions", type=int, default=512, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.plot_csv:
        with args.plot_csv.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        for row in rows:
            for metric in ("total_return", "sharpe", "max_drawdown"):
                row[metric] = None if not row[metric] else float(row[metric])
        plot_monitoring(rows, args.plot_file, args.plot_actions)
        return 0
    require(args.state is not None, "--state is required")
    state = read_json(args.state)
    if state.get("complete") is not True:
        print(json.dumps({"status": "not complete", "recorded_rounds": len(state.get("rounds", [])),
                          "final_audit_generated": False}))
        return 2
    protocol = read_json(args.protocol)
    config = read_json(ROOT / "workbench.local.json")
    workspace = args.workspace or protocol.get("workspace_path") or ROOT / config["workspace"]
    audit = Audit(state, protocol, workspace, args.state)
    try:
        result, rows = audit.run()
    finally:
        audit.db.close()
    output = (args.output or args.state.parent / "audit").resolve()
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "monitoring-summary.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    figure = output / "training-progress.png"
    if args.plot_python:
        subprocess.run([str(args.plot_python), str(Path(__file__).resolve()), "--plot-csv", str(csv_path), "--plot-file", str(figure),
                        "--plot-actions", str(protocol["timesteps_per_round"])], check=True)
    else:
        plot_monitoring(rows, figure, protocol["timesteps_per_round"])
    result["outputs"] = {"monitoring_csv": str(csv_path), "training_plot": str(figure)}
    temporary = output / "final-audit.json.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    os.replace(temporary, output / "final-audit.json")
    print(json.dumps({"status": "audited", "model_id": state["model_id"], "timesteps": result["timesteps"],
                      "valid_validation_selection": result["valid_validation_selection"],
                      "monitoring_trend": result["monitoring_trend"], "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
