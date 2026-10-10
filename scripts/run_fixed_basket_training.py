"""Run a fixed protocol through the local API, with durable continuation receipts.

Usage: uv run --no-sync python scripts/run_fixed_basket_training.py
       --protocol protocol.json --output outputs/fixed-basket --url http://127.0.0.1:8770
No failed or uncertain POST is automatically retried. Offline last.zip evaluations
are research files, not registered workbench backtests. Final results never select
checkpoints; policy.zip remains selected exclusively by the fixed validation rule.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import time
import zipfile

import httpx


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"failed", "cancelled", "interrupted"}


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def validate_manifest(manifest, model_id, parent_id, started, budget):
    expected = {"model_id": model_id, "parent_content_id": parent_id,
                "timesteps_started": started, "timesteps_completed": started + budget,
                "session_timesteps": budget}
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ValueError(f"continuation mismatch: {key}")


def ensure_device(device):
    if device not in {"cpu", "cuda"}:
        raise ValueError("device override must be cpu or cuda")
    if device == "cuda":
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA unavailable; CPU fallback is forbidden")
        torch.zeros(1, device="cuda")  # Fail before submitting any job if initialization fails.


class API:
    def __init__(self, url):
        self.client = httpx.Client(base_url=url.rstrip("/") + "/api/", timeout=60)
        response = self.client.get("session")
        response.raise_for_status()
        self.client.headers["Authorization"] = "Bearer " + response.json()["token"]

    def get(self, path):
        response = self.client.get(path.lstrip("/"))
        response.raise_for_status()
        return response.json()

    def post(self, path, body):
        response = self.client.post(path.lstrip("/"), json=body)
        response.raise_for_status()
        return response.json()

    def upload(self, path):
        with path.open("rb") as stream:
            response = self.client.post("uploads", files={"file": (path.name, stream, "application/zip")})
        response.raise_for_status()
        return response.json()["upload_id"]


class Runner:
    def __init__(self, protocol, output, url, *, api=None):
        self.protocol, self.output, self.url = protocol, Path(output).resolve(), url.rstrip("/")
        self.started = time.monotonic()
        self.output.mkdir(parents=True, exist_ok=True)
        self.state_path = self.output / "state.json"
        identity = digest({"protocol": protocol, "url": self.url})
        source = self.extension_source() if protocol.get("source_state_path") else None
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state["protocol_hash"] != identity:
                raise ValueError("protocol or URL changed; use a different output directory")
            if source and self.state.get("extension", {}).get("source_state_sha256") != protocol["source_state_sha256"]:
                raise ValueError("extension provenance changed")
        else:
            self.state = self.seed_extension(source, identity) if source else {
                "protocol_hash": identity, "operations": {}, "rounds": [], "evaluations": {}}
            self.save()
        self.elapsed_before = self.state.get("elapsed_seconds", 0.)
        self.api = api or API(self.url)
        workspace = protocol.get("workspace_path")
        if workspace is None:
            config = json.loads((ROOT / "workbench.local.json").read_text(encoding="utf-8"))
            workspace = ROOT / config["workspace"]
        self.workspace = Path(workspace).resolve()

    def extension_source(self):
        path = Path(self.protocol["source_state_path"]).resolve()
        if path.parent == self.output:
            raise ValueError("extension requires a new output directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.protocol.get("source_state_sha256"):
            raise ValueError("source state hash does not match pinned provenance")
        source = json.loads(raw)
        base_rounds = len(source.get("rounds", []))
        stopped = source.get("stop", {})
        if not (base_rounds == 50 and source.get("complete") is True or
                base_rounds == 105 and stopped.get("successful_rounds") == 105
                and "active_job_id" in stopped and stopped["active_job_id"] is None):
            raise ValueError("extension requires a completed 50-round or stopped successful 105-round source")
        if self.protocol.get("rounds") != base_rounds + 200 or self.protocol.get("timesteps_per_round") != 512:
            raise ValueError("extension requires 200 additional rounds at 512 steps")
        if base_rounds == 105 and self.protocol.get("monitor_start_round") != 115:
            raise ValueError("105-round extension monitoring must start at round 115")
        if self.protocol.get("device_override") != "cuda":
            raise ValueError("this extension requires explicit CUDA execution")
        from quanto.investing.contracts import PolicyTrainingSpec
        spec = PolicyTrainingSpec.model_validate(self.protocol["spec"]).model_dump(mode="json")
        if source["operations"]["model"]["body"] != {"dataset_id": source["dataset_id"], "spec": spec}:
            raise ValueError("extension must preserve original model specification and dataset")
        previous = None
        for number, entry in enumerate(source["rounds"], 1):
            if (entry["round"] != number or entry["timesteps_started"] != (number - 1) * 512
                    or entry["timesteps_completed"] != number * 512 or entry["session_timesteps"] != 512
                    or entry["base_version_id"] != previous):
                raise ValueError("source continuation chain is inconsistent")
            previous = entry["version_id"]
        expected = {f"monitor_{n:03d}_{snapshot}" for n in range(10, base_rounds + 1, 10) for snapshot in ("best", "last")}
        prefix = "final" if base_rounds == 50 else "historical_final_050"
        expected |= {"monitor_equal_weight", "monitor_buy_hold"}
        expected |= {prefix + "_" + snapshot for snapshot in ("best", "last", "equal_weight", "buy_hold")}
        if set(source["evaluations"]) != expected:
            raise ValueError("completed source evaluations are incomplete")
        return source

    def seed_extension(self, source, identity):
        path = Path(self.protocol["source_state_path"]).resolve()
        evaluations, operations = {}, {}
        for key, entry in source["evaluations"].items():
            summary = Path(entry.get("source_summary_path", path.parent / (key + ".json"))).resolve()
            if json.loads(summary.read_text(encoding="utf-8")) != entry["summary"]:
                raise ValueError("source evaluation summary changed")
            entry["source_summary_path"] = str(summary)
            if not entry.get("job_id"):
                equity = Path(entry.get("source_equity_path", path.parent / (key + ".equity.csv"))).resolve()
                if key.endswith("_last") and not equity.is_file():
                    raise ValueError("source offline equity file missing")
                if equity.is_file():
                    entry["source_equity_path"] = str(equity)
            evaluations[f"historical_final_{entry['round']:03d}_" + key[6:] if key.startswith("final_") else key] = entry
        for key, receipt in source["operations"].items():
            operations["historical_final_050_" + key[6:] if key.startswith("final_") else key] = receipt
        last = source["rounds"][-1]
        return {"protocol_hash": identity, "operations": operations, "rounds": source["rounds"],
                "evaluations": evaluations, "model_id": source["model_id"], "dataset_id": source["dataset_id"],
                "complete": False, "extension": {"source_state_path": str(path),
                    "source_state_sha256": self.protocol["source_state_sha256"], "source_protocol_hash": source["protocol_hash"],
                    "base_rounds": len(source["rounds"]), "base_version_id": last["version_id"], "base_content_id": last["content_id"],
                    "model_id": source["model_id"], "dataset_id": source["dataset_id"],
                    "device_override": self.protocol["device_override"], "source_elapsed_seconds": source.get("elapsed_seconds")}}

    def final_prefix(self):
        return f"final_{self.protocol['rounds']}" if self.state.get("extension") else "final"

    def verify_extension(self, model, budget):
        if not self.state.get("extension"):
            return
        from quanto.learning.artifacts import read_manifest
        sessions = {entry["id"]: entry for entry in model["sessions"]}
        versions = {entry["id"]: entry["manifest"] for entry in model["versions"]}
        previous_id = previous_content = None
        for number, entry in enumerate(self.state["rounds"], 1):
            session = sessions.get(entry["version_id"])
            if not session or session["state"] != "succeeded" or session["base_version_id"] != previous_id:
                raise RuntimeError("extension recorded session/parent is not successful")
            manifest = versions[entry["version_id"]]
            validate_manifest(manifest, self.state["model_id"], previous_content, (number - 1) * budget, budget)
            if manifest["id"] != entry["content_id"] or manifest["dataset_content_hash"] != self.dataset_hash:
                raise RuntimeError("extension dataset or content identity mismatch")
            previous_id, previous_content = entry["version_id"], manifest["id"]
        row = self.db_row("learning_artifacts", previous_id)
        if read_manifest(Path(row["path"]), "policy")["id"] != previous_content:
            raise RuntimeError("extension checkpoint files changed")
        pending = self.state["operations"].get(f"train_{len(self.state['rounds']) + 1:03d}")
        allowed_ids = {previous_id}
        if pending:
            recovered = [pending["id"]] if pending.get("id") else self.recover(pending)
            if len(recovered) != 1:
                raise RuntimeError("ambiguous pending extension session; no POST repeated")
            allowed_ids.update(recovered)
        if model["latest_version_id"] not in allowed_ids or (model.get("active_job_id") and model["active_job_id"] not in allowed_ids):
            raise RuntimeError("extension model head changed outside this runner")
        for entry in self.state["evaluations"].values():
            if entry.get("source_summary_path") and entry.get("job_id"):
                registered = self.db_row("learning_artifacts", entry["job_id"])
                entry["source_equity_path"] = str((Path(registered["path"]) / "equity.csv").resolve())
        self.save()

    def save(self):
        if hasattr(self, "elapsed_before"):
            self.state["elapsed_seconds"] = round(self.elapsed_before + time.monotonic() - self.started, 3)
        atomic_json(self.state_path, self.state)

    def log(self, message):
        line = f"{datetime.now(timezone.utc).isoformat()} {message}"
        print(line, flush=True)
        with (self.output / "run.log").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def listing(self, path):
        items, cursor = [], None
        while True:
            page = self.api.get(path + "?limit=100" + ("&cursor=" + cursor if cursor else ""))
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                return items

    def db_row(self, table, identity):
        if table not in {"datasets", "jobs", "learning_artifacts"}:
            raise ValueError("unsupported local lookup")
        database = self.workspace / "workbench.sqlite3"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise RuntimeError("local workspace does not contain API resource " + identity)
        return dict(row)

    def recover(self, operation):
        before = set(operation["before_ids"])
        if operation["kind"] == "model":
            return [item["id"] for item in self.listing("/investing/models")
                    if item["id"] not in before and item["dataset_id"] == operation["body"]["dataset_id"]
                    and item["spec"] == operation["body"]["spec"]]
        if operation["kind"] == "train":
            model = self.api.get("/investing/models/" + self.state["model_id"])
            return [item["id"] for item in model["sessions"] if item["id"] not in before
                    and item["base_version_id"] == operation["base_version_id"]]
        matches = []
        for job in self.listing("/jobs"):
            if job["id"] in before:
                continue
            expected_kind = "import" if operation["kind"] == "import" else "policy_backtest"
            if job["kind"] != expected_kind:
                continue
            row = self.db_row("jobs", job["id"])
            payload = json.loads(Path(row["input_path"]).read_text(encoding="utf-8"))["input"]
            request = payload.get("request", payload)
            body = operation["body"]
            if operation["kind"] == "import":
                valid = request.get("upload_id") == body["upload_id"]
            else:
                valid = (request.get("model_id") == self.state["model_id"]
                         and request.get("policy_id") == body["version_id"]
                         and request.get("dataset_id") == body["dataset_id"]
                         and request.get("spec") == body["spec"])
            if valid:
                matches.append(job["id"])
        return matches

    def submit(self, key, path, body, kind, base_version_id=None):
        operation = self.state["operations"].get(key)
        if operation and operation.get("id"):
            return operation["id"]
        if operation:
            matches = self.recover(operation)
            if len(matches) != 1:
                raise RuntimeError(f"ambiguous submission {key}: {len(matches)} matches; "
                                   "inspect state/API before manual recovery; POST was not repeated")
            operation.update(id=matches[0], status="submitted")
            self.save()
            self.log(f"recovered {key}: {matches[0]}")
            return matches[0]
        if kind == "train":
            model = self.api.get("/investing/models/" + self.state["model_id"])
            if model.get("latest_version_id") != base_version_id:
                raise RuntimeError("model head changed outside this runner")
            before = [item["id"] for item in model["sessions"]]
        else:
            before = [item["id"] for item in self.listing("/investing/models" if kind == "model" else "/jobs")]
        operation = {"kind": kind, "path": path, "body": body, "before_ids": before,
                     "base_version_id": base_version_id, "status": "submitting"}
        self.state["operations"][key] = operation
        self.save()  # Intent precedes the non-idempotent POST, even if its response is lost.
        result = self.api.post(path, body)
        operation.update(id=result["id"], status="submitted")
        self.save()  # Job ID is durable before any polling.
        self.log(f"submitted {key}: {result['id']}")
        return result["id"]

    def wait(self, job_id):
        previous, announced = None, 0.
        while True:
            job = self.api.get("/jobs/" + job_id)
            status = job["state"]
            now = time.monotonic()
            if status != previous or now - announced >= 30:
                self.log(f"job {job_id}: {status}")
                previous, announced = status, now
            if status == "succeeded":
                return job
            if status in TERMINAL:
                self.state["last_failure"] = {"job_id": job_id, "state": status, "error": job.get("error")}
                self.save()
                raise RuntimeError(f"job {job_id} {status}; no automatic retry")
            time.sleep(5)

    def dataset(self):
        if self.state.get("dataset_id"):
            identity = self.state["dataset_id"]
        else:
            source = Path(self.protocol["dataset_path"]).resolve()
            if not self.state.get("upload_id"):
                if source.is_dir():
                    if self.output.is_relative_to(source):
                        raise ValueError("output directory must be outside the source dataset directory")
                    archive = self.output / "dataset.zip"
                    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                        for path in sorted(source.rglob("*")):
                            if path.is_file():
                                bundle.write(path, path.relative_to(source).as_posix())
                else:
                    archive = source
                self.state["upload_id"] = self.api.upload(archive)
                self.save()
            job_id = self.submit("import", "/datasets", {"upload_id": self.state["upload_id"]}, "import")
            job = self.wait(job_id)
            identity = job.get("result", {}).get("dataset_version_id", job_id)
            self.api.get("/datasets/" + identity)
            self.state["dataset_id"] = identity
            self.save()
        detail = self.api.get("/datasets/" + identity)
        row = self.db_row("datasets", identity)
        if row["content_hash"] != detail["content_hash"]:
            raise RuntimeError("API/local dataset identity mismatch")
        self.dataset_path = Path(row["path"])
        self.dataset_hash = detail["content_hash"]
        return identity

    def comparison(self):
        rows = []
        for item in self.state["rounds"]:
            for snapshot in ("latest", "best"):
                rows.append({"round": item["round"], "period": "validation", "snapshot": snapshot,
                             "version_id": item["version_id"], **item["validation_" + snapshot]})
        for key, item in self.state["evaluations"].items():
            rows.append({"evaluation": key, "round": item["round"], "period": item["period"],
                         "snapshot": item["snapshot"], "version_id": item["version_id"],
                         **item["summary"]["metrics"]})
        atomic_json(self.output / "comparison.json", {"rounds": self.state["rounds"], "evaluations": rows})
        columns = sorted({name for row in rows for name in row})
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        path = self.output / "comparison.csv"
        temporary = path.with_suffix(".csv.tmp")
        temporary.write_text(buffer.getvalue(), encoding="utf-8-sig")
        os.replace(temporary, path)

    def backtest(self, key, round_number, version_id, period, dates, baseline="policy"):
        if key in self.state["evaluations"]:
            return
        body = {"dataset_id": self.state["dataset_id"], "version_id": version_id,
                "spec": {**dates, "baseline": baseline, "cost_multiplier": 1}}
        job_id = self.submit(key, "/investing/models/" + self.state["model_id"] + "/backtests", body, "backtest")
        self.wait(job_id)
        summary = self.api.get("/investing/backtests/" + job_id)["manifest"]
        atomic_json(self.output / (key + ".json"), summary)
        self.state["evaluations"][key] = {"round": round_number, "period": period, "version_id": version_id,
                                           "snapshot": "validation_best" if baseline == "policy" else baseline,
                                           "job_id": job_id, "summary": summary}
        self.save()
        self.comparison()

    def offline_last(self, key, round_number, version_id, period, dates):
        if key in self.state["evaluations"]:
            return
        from stable_baselines3 import PPO
        from quanto.backtest.dataset_loaders import load_dataset
        from quanto.config import BacktestSpec
        from quanto.investing.contracts import PolicyTrainingSpec
        from quanto.investing.evaluation import evaluate_policy
        from quanto.learning.artifacts import read_manifest

        self.log(f"offline last.zip {period} round {round_number}: evaluating")
        row = self.db_row("learning_artifacts", version_id)
        directory = Path(row["path"])
        manifest = read_manifest(directory, "policy")
        config = PolicyTrainingSpec.model_validate(manifest["config"])
        if manifest["model_id"] != self.state["model_id"] or manifest["dataset_content_hash"] != self.dataset_hash:
            raise RuntimeError("offline evaluation identity mismatch")
        values = config.backtest.model_dump(mode="json")
        values.update(dates)
        spec = BacktestSpec.model_validate(values)
        device = self.protocol.get("device_override", config.device)
        ensure_device(device)
        model = PPO.load(directory / "last.zip", device=device)
        if device == "cuda" and model.device.type != "cuda":
            raise RuntimeError("CUDA inference was not honored; CPU fallback is forbidden")
        metrics, result, report = evaluate_policy(model, load_dataset(self.dataset_path), spec,
                                                   config.frequency, config.lookback,
                                                   action_version=config.action_version)
        summary = {"kind": "offline_last_evaluation", "registered_in_workbench": False,
                   "version_id": version_id, "policy_content_id": manifest["id"], "period": dates,
                   "snapshot": "last.zip", "runtime": {"device": str(model.device)}, "metrics": {**report.metrics, **metrics},
                   "metric_definitions": report.metric_definitions, "selection_use": "none",
                   "assumptions": {"execution": spec.execution.model_dump(mode="json"),
                                   "price_mode": manifest["price_mode"], "limitations": report.warnings}}
        temporary = self.output / (key + ".equity.csv.tmp")
        result.equity.to_csv(temporary, index=False, encoding="utf-8")
        os.replace(temporary, self.output / (key + ".equity.csv"))
        atomic_json(self.output / (key + ".json"), summary)
        self.state["evaluations"][key] = {"round": round_number, "period": period, "version_id": version_id,
                                           "snapshot": "last.zip_offline", "summary": summary}
        self.save()
        self.comparison()
        self.log(f"offline last.zip {period} round {round_number}: complete")

    def run(self):
        from quanto.investing.contracts import PolicyTrainingSpec

        config = PolicyTrainingSpec.model_validate(self.protocol["spec"])
        spec = config.model_dump(mode="json")
        rounds, budget = self.protocol["rounds"], self.protocol["timesteps_per_round"]
        monitor, final = self.protocol["monitor_period"], self.protocol["final_period"]
        monitor_start = self.protocol.get("monitor_start_round", 10)
        if type(monitor_start) is not int or monitor_start < 10 or monitor_start > rounds:
            raise ValueError("monitor_start_round must be an integer from 10 through the target round")
        if (type(rounds) is not int or rounds < 50 or type(budget) is not int or budget % config.n_steps
                or budget < config.n_steps):
            raise ValueError("require at least 50 rounds and a positive complete-rollout budget")
        if not (str(config.validation_end) < monitor["start"] < monitor["end"] < final["start"] < final["end"]):
            raise ValueError("validation, monitoring and final periods must be ordered and disjoint")
        if final != {"start": str(config.test_start), "end": str(config.test_end)}:
            raise ValueError("final period must equal the fixed test partition")
        device = self.protocol.get("device_override", config.device)
        ensure_device(device)
        dataset_id = self.dataset()
        if not self.state.get("model_id"):
            self.state["model_id"] = self.submit("model", "/investing/models", {"dataset_id": dataset_id, "spec": spec}, "model")
            self.save()
        model_id = self.state["model_id"]
        model = self.api.get("/investing/models/" + model_id)
        if model["spec"] != spec or model["dataset_id"] != dataset_id:
            raise RuntimeError("saved model configuration differs from protocol")
        self.verify_extension(model, budget)
        for number in range(1, rounds + 1):
            if len(self.state["rounds"]) < number:
                round_started = time.monotonic()
                previous = self.state["rounds"][-1] if self.state["rounds"] else None
                base = previous["version_id"] if previous else None
                self.log(f"round {number}/{rounds}: append {budget} timesteps")
                job_id = self.submit(f"train_{number:03d}", f"/investing/models/{model_id}/train",
                                     {"additional_timesteps": budget, "device": device}, "train", base)
                self.wait(job_id)
                model = self.api.get("/investing/models/" + model_id)
                session = next(item for item in model["sessions"] if item["id"] == job_id)
                if session["base_version_id"] != base or model["latest_version_id"] != job_id:
                    raise RuntimeError("saved session parent/head changed")
                manifest = self.api.get("/investing/policies/" + job_id)["manifest"]
                validate_manifest(manifest, model_id, previous["content_id"] if previous else None,
                                  previous["timesteps_completed"] if previous else 0, budget)
                if manifest.get("metrics", {}).get("test") is not None:
                    raise RuntimeError("training unexpectedly evaluated the final test")
                if device == "cuda" and (manifest["config"]["device"] != "cuda" or manifest["runtime"]["device"] != "cuda"):
                    raise RuntimeError("CUDA training was not honored; CPU fallback is forbidden")
                if self.protocol.get("require_sampling_algorithm"):
                    expected_sampling = dict(algorithm=self.protocol["require_sampling_algorithm"], base_seed=config.seed,
                        parent_timesteps=manifest["timesteps_started"],
                        effective_env_seed=config.seed + manifest["timesteps_started"])
                    if manifest.get("sampling_provenance") != expected_sampling:
                        raise RuntimeError("required continuation sampling algorithm was not honored")
                latest = manifest["history"][-1]
                latest_metrics = {key.removeprefix("validation_"): value for key, value in latest.items()
                                  if key.startswith("validation_")}
                entry = {"round": number, "version_id": job_id, "content_id": manifest["id"],
                         "base_version_id": base, "validation_latest": latest_metrics,
                         "validation_best": manifest["metrics"]["validation"],
                         "elapsed_seconds": round(time.monotonic() - round_started, 3),
                         **{key: manifest.get(key) for key in ("timesteps_started", "timesteps_completed",
                            "session_timesteps", "best_timesteps", "best_source_content_id", "selection_valid", "sampling_provenance")}}
                self.state["rounds"].append(entry)
                self.save()
                atomic_json(self.output / f"round_{number:03d}.json", manifest)
                self.comparison()
                self.log(f"round {number}/{rounds}: saved cumulative timesteps {manifest['timesteps_completed']}; "
                         f"round_elapsed_seconds={entry['elapsed_seconds']}; "
                         f"run_elapsed_seconds={self.state['elapsed_seconds']}")
            version = self.state["rounds"][number - 1]["version_id"]
            if number >= monitor_start and (number - monitor_start) % 10 == 0:
                self.backtest(f"monitor_{number:03d}_best", number, version, "monitor", monitor)
                self.offline_last(f"monitor_{number:03d}_last", number, version, "monitor", monitor)
                for baseline in ("equal_weight", "buy_hold"):
                    self.backtest("monitor_" + baseline, number, version, "monitor", monitor, baseline)
        version = self.state["rounds"][-1]["version_id"]
        final_prefix = self.final_prefix()
        self.backtest(final_prefix + "_best", rounds, version, "final", final)
        self.offline_last(final_prefix + "_last", rounds, version, "final", final)
        for baseline in ("equal_weight", "buy_hold"):
            self.backtest(final_prefix + "_" + baseline, rounds, version, "final", final, baseline)
        self.state["complete"] = True
        self.save()
        self.comparison()
        self.log(f"complete: {rounds} successful rounds; elapsed_seconds={self.state['elapsed_seconds']}; "
                 "final results did not select checkpoints")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8770")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # An OS lock is released on process exit, so crashes do not leave a stale lock.
    with (args.output / "runner.lock").open("a+b") as lock:
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
        protocol = json.loads(args.protocol.read_text(encoding="utf-8-sig"))
        current = Runner(protocol, args.output, args.url)
        try:
            current.run()
        except (Exception, KeyboardInterrupt) as exc:
            # Do not print HTTP bodies, headers, or the session token.
            current.log(f"stopped: {type(exc).__name__}; inspect state.json and the recorded job IDs")
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
