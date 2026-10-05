"""Run two fresh paired reward chains; compare only after both reach 300 rounds.

Usage: uv run --no-sync python scripts/run_reward_trial.py --config run.json --output outputs/trial
Receipts precede mutations. Failed or uncertain requests are never blindly retried.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from scripts.run_fixed_basket_training import API, Runner, atomic_json, digest, ensure_device, validate_manifest
from scripts.compare_policy_rewards import CONTROL_REWARD, DSR_REWARD, compare_trial
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import check_policy_identity
from quanto.learning.artifacts import read_manifest

ROLES = ("control", "dsr")
SAMPLING = "base-seed-plus-parent-timesteps-v1"


class RewardTrialRunner:
    def __init__(self, config, output, *, api=None):
        self.config, self.output = config, Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.trial_path = Path(config["trial_path"]).resolve()
        raw = self.trial_path.read_bytes()
        self.trial = json.loads(raw.decode("utf-8-sig"))
        identity = digest(dict(config=config, trial_sha256=hashlib.sha256(raw).hexdigest()))
        self.state_path = self.output / "state.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state["identity"] != identity:
                raise ValueError("configuration or trial bytes changed; use a new output directory")
        else:
            self.state = dict(identity=identity, config=config, trial_sha256=hashlib.sha256(raw).hexdigest(),
                              status="initializing", roles={})
            atomic_json(self.state_path, self.state)
        self.specs = self.validate_trial()
        self.api = api or API(config["url"])
        self.runners = {}
        for role in ROLES:
            protocol = dict(trial=self.trial, trial_sha256=self.state["trial_sha256"], role=role,
                spec=self.specs[role], workspace_path=config["workspace_path"],
                dataset_id=config["dataset_id"], rounds=config["rounds"],
                timesteps_per_round=config["timesteps_per_round"], device_override="cuda",
                require_sampling_algorithm=SAMPLING, run_config=config)
            self.runners[role] = Runner(protocol, self.output / role, config["url"], api=self.api)

    def validate_trial(self):
        if type(self.config["rounds"]) is not int or self.config["rounds"] != 300:
            raise ValueError("paired reward trial requires exactly 300 successful rounds per role")
        if type(self.config["timesteps_per_round"]) is not int or self.config["timesteps_per_round"] != 512:
            raise ValueError("paired reward trial requires 512 decision steps per round")
        if set(self.trial.get("specs", {})) != set(ROLES):
            raise ValueError("trial must contain exactly control and dsr specs")
        specs = {}
        for role, reward in zip(ROLES, (CONTROL_REWARD, DSR_REWARD)):
            spec = PolicyTrainingSpec.model_validate(self.trial["specs"][role]).model_dump(mode="json")
            if (spec["reward_version"] != reward or spec["test_start"] is not None
                    or spec["test_end"] is not None or spec["selection_metric"] != "validation_sharpe"
                    or spec["eval_every_timesteps"] != 512 or 512 % spec["n_steps"]
                    or len(spec["backtest"]["symbols"]) != 32 or len(set(spec["backtest"]["symbols"])) != 32
                    or spec["validation_start"] != self.trial["validation_start"]
                    or spec["validation_end"] != self.trial["validation_end"]):
                raise ValueError("trial must preserve the 32-ETF basket, reward and 512-step validation protocol")
            specs[role] = spec
        fixed = [{key: value for key, value in specs[role].items() if key not in {"name", "reward_version"}}
                 for role in ROLES]
        if fixed[0] != fixed[1]:
            raise ValueError("paired specs differ beyond name and reward version")
        return specs

    def save(self):
        for role, runner in self.runners.items():
            rounds = runner.state["rounds"]
            ids = [op.get("id") for op in runner.state["operations"].values() if op["kind"] == "train"]
            self.state["roles"][role] = dict(model_id=runner.state.get("model_id"),
                successful_rounds=len(rounds), last_version_id=rounds[-1]["version_id"] if rounds else None,
                last_job_id=ids[-1] if ids else None, timesteps_completed=rounds[-1]["timesteps_completed"] if rounds else 0)
        atomic_json(self.state_path, self.state)

    def log(self, message):
        line = f"{datetime.now(timezone.utc).isoformat()} {message}"
        print(line, flush=True)
        with (self.output / "run.log").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def check_manifest(self, role, manifest, previous, number):
        runner, spec = self.runners[role], self.specs[role]
        validate_manifest(manifest, runner.state["model_id"], previous["content_id"] if previous else None,
                          (number - 1) * 512, 512)
        if manifest.get("dataset_content_hash") != self.trial["dataset_content_hash"]:
            raise RuntimeError("policy dataset identity differs from frozen trial")
        config = manifest.get("config", {})
        fixed = lambda values: {key: value for key, value in values.items() if key not in {"device", "total_timesteps"}}
        if fixed(config) != fixed(spec):
            raise RuntimeError("policy configuration differs from frozen trial")
        check_policy_identity(manifest, PolicyTrainingSpec.model_validate(spec))
        runtime = manifest.get("runtime")
        if (config.get("device") != "cuda" or not isinstance(runtime, dict) or runtime.get("device") != "cuda"
                or config.get("total_timesteps") != 512):
            raise RuntimeError("CUDA or actual session budget was not honored")
        expected = dict(algorithm=SAMPLING, base_seed=spec["seed"], parent_timesteps=(number - 1) * 512,
                        effective_env_seed=spec["seed"] + (number - 1) * 512)
        if manifest.get("sampling_provenance") != expected:
            raise RuntimeError("continuation sampling identity differs from protocol")
        if (manifest.get("selection_metric") != "validation_sharpe"
                or "test" not in manifest.get("metrics", {}) or manifest["metrics"]["test"] is not None):
            raise RuntimeError("training selection or absent test partition differs from protocol")
        if not {"policy.zip", "last.zip"}.issubset(manifest.get("files", {})):
            raise RuntimeError("selected or latest policy checkpoint is missing")

    def model(self, role):
        runner = self.runners[role]
        model = self.api.get("/investing/models/" + runner.state["model_id"])
        if (model.get("id") != runner.state["model_id"] or model["spec"] != self.specs[role]
                or model["dataset_id"] != self.config["dataset_id"]):
            raise RuntimeError("saved model identity or configuration differs from protocol")
        return model

    def verify_chain(self, role):
        runner = self.runners[role]
        model = self.model(role)
        sessions = {item["id"]: item for item in model["sessions"]}
        versions = {item["id"]: item["manifest"] for item in model["versions"]}
        previous = None
        for number, entry in enumerate(runner.state["rounds"], 1):
            session = sessions.get(entry["version_id"], {})
            base = previous["version_id"] if previous else None
            if entry["round"] != number or entry["base_version_id"] != base or session.get("state") != "succeeded" or session.get("base_version_id") != base:
                raise RuntimeError("recorded continuation chain is inconsistent")
            manifest = versions.get(entry["version_id"], {})
            self.check_manifest(role, manifest, previous, number)
            if any(entry[key] != manifest.get(key) for key in ("timesteps_started", "timesteps_completed", "session_timesteps")) or entry["content_id"] != manifest["id"]:
                raise RuntimeError("recorded policy content or step count changed")
            receipt = runner.state["operations"].get(f"train_{number:03d}", {})
            if receipt.get("id") != entry["version_id"]:
                raise RuntimeError("recorded round does not match its durable submission receipt")
            previous = entry
        if len(runner.state["rounds"]) > self.config["rounds"]:
            raise RuntimeError("recorded chain exceeds the fixed training budget")
        pending = runner.state["operations"].get(f"train_{len(runner.state['rounds']) + 1:03d}")
        pending_id = None
        if pending:
            matches = [pending["id"]] if pending.get("id") else runner.recover(pending)
            if len(matches) != 1:
                raise RuntimeError("ambiguous pending submission; POST was not repeated")
            pending_id = matches[0]
            if sessions.get(pending_id, {}).get("base_version_id") != (previous["version_id"] if previous else None):
                raise RuntimeError("pending session parent differs from recorded chain")
        known = {entry["version_id"] for entry in runner.state["rounds"]}
        if pending_id:
            known.add(pending_id)
        heads = {previous["version_id"] if previous else None, pending_id}
        if (set(sessions) != known or model.get("latest_version_id") not in heads
                or model.get("active_job_id") not in {None, pending_id}):
            raise RuntimeError("model head or sessions changed outside this runner")
        if previous:
            local = read_manifest(Path(runner.db_row("learning_artifacts", previous["version_id"])["path"]), "policy")
            if local != versions[previous["version_id"]]:
                raise RuntimeError("local checkpoint differs from API artifact")
        return model

    def train_round(self, role, number):
        runner = self.runners[role]
        if len(runner.state["rounds"]) >= number:
            return
        if len(runner.state["rounds"]) != number - 1:
            raise RuntimeError("round sequence is inconsistent")
        previous = runner.state["rounds"][-1] if runner.state["rounds"] else None
        base = previous["version_id"] if previous else None
        job_id = runner.submit(f"train_{number:03d}", f"/investing/models/{runner.state['model_id']}/train",
                               {"additional_timesteps": 512, "device": "cuda"}, "train", base)
        runner.state["last_job_id"] = job_id
        runner.save()
        self.save()
        runner.wait(job_id)
        model = self.model(role)
        session = next(item for item in model["sessions"] if item["id"] == job_id)
        if session["state"] != "succeeded" or session["base_version_id"] != base or model["latest_version_id"] != job_id:
            raise RuntimeError("successful session parent or model head changed")
        manifest = self.api.get("/investing/policies/" + job_id)["manifest"]
        self.check_manifest(role, manifest, previous, number)
        local = read_manifest(Path(runner.db_row("learning_artifacts", job_id)["path"]), "policy")
        if local != manifest:
            raise RuntimeError("local checkpoint differs from API artifact")
        atomic_json(runner.output / f"round_{number:03d}.json", manifest)
        runner.state["rounds"].append(dict(round=number, version_id=job_id, content_id=manifest["id"],
            base_version_id=base, validation_best=manifest["metrics"]["validation"],
            **{key: manifest[key] for key in ("timesteps_started", "timesteps_completed", "session_timesteps", "sampling_provenance")}))
        runner.save()
        self.save()
        runner.log(f"round {number}/300 saved; cumulative decision steps {number * 512}")

    def verify_final(self):
        directory = self.output / "final"
        report = json.loads((directory / "comparison.json").read_text(encoding="utf-8"))
        period = dict(start=self.trial["validation_start"], end=self.trial["validation_end"])
        if (report.get("kind") != "policy_reward_comparison" or report.get("dataset_content_hash") != self.trial["dataset_content_hash"]
                or report.get("validation_period") != period
                or report.get("training") != dict(timesteps_completed=153600, session_timesteps=512)):
            raise RuntimeError("final comparison identity differs from frozen trial")
        for role, runner in self.runners.items():
            last = runner.state["rounds"][-1]
            result = report["results"][role]
            summary = json.loads((directory / role / "summary.json").read_text(encoding="utf-8"))
            if (result.get("model_id") != runner.state["model_id"] or result.get("policy_content_id") != last["content_id"]
                    or result.get("timesteps_completed") != 153600 or result.get("session_timesteps") != 512
                    or summary.get("kind") != "policy_validation" or summary.get("evaluation_purpose") != "model_selection"
                    or summary.get("policy_content_id") != last["content_id"] or summary.get("period") != period
                    or summary.get("training_dataset_content_hash") != self.trial["dataset_content_hash"]
                    or not (directory / role / "equity.csv").is_file()):
                raise RuntimeError("final validation report identity or outputs are incomplete")
        hashes = {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(directory.rglob("*")) if path.is_file()}
        if self.state.get("final_files") is not None and self.state["final_files"] != hashes:
            raise RuntimeError("completed final comparison files changed")
        self.state.update(final_files=hashes, final_report=str(directory / "comparison.json"), status="complete")
        self.save()

    def run(self):
        try:
            ensure_device("cuda")
            for role, runner in self.runners.items():
                if runner.state.get("dataset_id", self.config["dataset_id"]) != self.config["dataset_id"]:
                    raise RuntimeError("registered dataset ID changed")
                runner.state["dataset_id"] = self.config["dataset_id"]
                runner.save()
                runner.dataset()
                if runner.dataset_hash != self.trial["dataset_content_hash"]:
                    raise RuntimeError("registered dataset differs from trial")
                if not runner.state.get("model_id"):
                    runner.state["model_id"] = runner.submit("model", "/investing/models",
                        {"dataset_id": self.config["dataset_id"], "spec": self.specs[role]}, "model")
                    runner.save()
                if runner.state["operations"]["model"].get("id") != runner.state["model_id"]:
                    raise RuntimeError("model identity differs from its fresh creation receipt")
                self.verify_chain(role)
                self.save()
            if self.runners["control"].state["model_id"] == self.runners["dsr"].state["model_id"]:
                raise RuntimeError("paired trial requires two distinct fresh models")
            counts = [len(self.runners[role].state["rounds"]) for role in ROLES]
            if counts[0] < counts[1] or counts[0] > counts[1] + 1:
                raise RuntimeError("paired round counts violate alternating order")
            if self.state["status"] == "complete":
                self.verify_final()
                return
            self.state["status"] = "training"
            self.save()
            for number in range(1, 301):
                for role in ROLES:
                    self.train_round(role, number)
            if not all(len(runner.state["rounds"]) == 300 for runner in self.runners.values()):
                raise RuntimeError("both chains must finish before final comparison")
            final = self.output / "final"
            if not self.state.get("comparison_started"):
                if final.exists() and any(final.iterdir()):
                    raise RuntimeError("unrecorded final output exists; comparison not repeated")
                self.state.update(status="comparing", comparison_started=True)
                self.save()
                paths = [Path(self.runners[role].db_row("learning_artifacts", self.runners[role].state["rounds"][-1]["version_id"])["path"])
                         for role in ROLES]
                compare_trial(self.trial_path, *paths, final)
            if not (final / "comparison.json").is_file():
                raise RuntimeError("final comparison was interrupted; evaluation not repeated")
            self.verify_final()
            self.log("complete: both reward chains reached 300 successful rounds; final comparison verified")
        except (Exception, KeyboardInterrupt) as exc:
            self.state.update(status="stopped", stop={"error_type": type(exc).__name__})
            self.save()
            self.log(f"stopped: {type(exc).__name__}; inspect durable role state and job IDs")
            raise


@contextmanager
def controller_lock(output):
    with (output / "runner.lock").open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            if lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "controller.log").open("a", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            try:
                with controller_lock(args.output):
                    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
                    RewardTrialRunner(config, args.output).run()
            except (Exception, KeyboardInterrupt) as exc:
                print(f"stopped: {type(exc).__name__}; inspect state.json and role receipts", flush=True)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
