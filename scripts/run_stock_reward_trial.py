"""Run the two frozen stock PPO reward chains with durable, resumable receipts.

Usage: uv run --no-sync python scripts/run_stock_reward_trial.py \
    --protocol protocol.json --output outputs/stock-rewards --url http://127.0.0.1:8770
An uncertain or failed submission stops the controller; it is never blindly retried.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_fixed_basket_training import API, Runner, atomic_json, digest, ensure_device, validate_manifest
from quanto.investing.evaluation import check_policy_identity
from quanto.learning.artifacts import read_manifest
from quanto.workbench.framework_requests import ModelCreateRequest


ROLES = ("control", "dsr")
REWARDS = {"control": ("stock-ppo-cash-state-log", "net-log-return-with-state-v1"),
           "dsr": ("stock-ppo-cash-dsr", "differential-sharpe-v1")}
ROUNDS = 600
BUDGET = 1024


@contextmanager
def controller_lock(output):
    with (output / "runner.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            if lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def requests_from_protocol(protocol):
    if set(protocol) != {"dataset_id", "dataset_content_hash", "workspace_path", "basket_revision_ref",
                         "framework_contract_version", "trading_definition_ref", "models", "rounds",
                         "timesteps_per_round", "device"}:
        raise ValueError("stock protocol has missing or unexpected fields")
    if type(protocol["rounds"]) is not int or protocol["rounds"] != ROUNDS or type(protocol["timesteps_per_round"]) is not int or protocol["timesteps_per_round"] != BUDGET:
        raise ValueError("stock trial requires exactly 600 successful 1024-step sessions per method")
    if protocol["framework_contract_version"] != "multi-asset-v1" or protocol["trading_definition_ref"] != {
            "kind": "trading", "id": "stock-daily", "version": "1"}:
        raise ValueError("stock protocol requires the multi-asset contract and stock-daily@1")
    if type(protocol["dataset_content_hash"]) is not str or len(protocol["dataset_content_hash"]) != 64:
        raise ValueError("dataset content hash must be pinned")
    if set(protocol["models"]) != set(ROLES):
        raise ValueError("stock protocol requires exactly control and dsr models")
    requests = {}
    for role in ROLES:
        model = protocol["models"][role]
        if set(model) != {"learning_definition_ref", "spec"}:
            raise ValueError(f"{role} model requires learning_definition_ref and spec")
        definition, reward = REWARDS[role]
        if model["learning_definition_ref"] != {"kind": "learning", "id": definition, "version": "1"}:
            raise ValueError(f"{role} uses the wrong stock learning definition")
        request = ModelCreateRequest.model_validate({
            "framework_contract_version": protocol["framework_contract_version"],
            "basket_revision_ref": protocol["basket_revision_ref"],
            "dataset_id": protocol["dataset_id"],
            "trading_definition_ref": protocol["trading_definition_ref"],
            **model,
        }).model_dump(mode="json")
        spec = request["spec"]
        if (spec["reward_version"] != reward or spec["action_version"] != "hold-target-cash-quartiles-v2"
                or spec["device"] != protocol["device"] or BUDGET % spec["n_steps"]
                or spec["total_timesteps"] != BUDGET):
            raise ValueError(f"{role} training spec violates the fixed stock reward protocol")
        requests[role] = request
    comparable = [{key: value for key, value in requests[role]["spec"].items()
                   if key not in {"name", "reward_version"}} for role in ROLES]
    if comparable[0] != comparable[1] or requests["control"]["spec"]["name"] == requests["dsr"]["spec"]["name"]:
        raise ValueError("paired stock methods must share all spec fields except distinct names and rewards")
    return requests


class StockRunner(Runner):
    """Reuse the generic write-ahead receipts, with stock model recovery."""

    def recover(self, operation):
        if operation["kind"] == "train":
            model = self.api.get("/investing/models/" + self.state["model_id"] + "?include_history=false")
            if model["session_count"] != operation["before_count"] + 1:
                return []
            page = self.api.get(f"/investing/models/{self.state['model_id']}/history"
                                f"?kind=sessions&session_number={operation['before_count'] + 1}&page_size=1")
            return [item["id"] for item in page["items"]
                    if item["base_version_id"] == operation["base_version_id"]]
        if operation["kind"] != "model":
            return super().recover(operation)
        body = operation["body"]
        matches = []
        for item in self.listing("/investing/models"):
            if item["id"] in operation["before_ids"] or item["dataset_id"] != body["dataset_id"]:
                continue
            binding = item.get("framework_binding", {})
            spec = {**item["spec"], "backtest": dict(item["spec"]["backtest"])}
            symbols = spec.get("backtest", {}).pop("symbols", None)
            pairs = binding.get("ordered_pairs", [])
            refs = [entry.get("ref") for entry in binding.get("definitions", [])]
            if (spec == body["spec"] and symbols == [entry["local_symbol"] for entry in pairs]
                    and binding.get("basket_revision_ref") == body["basket_revision_ref"]
                    and body["learning_definition_ref"] in refs and body["trading_definition_ref"] in refs):
                matches.append(item["id"])
        return matches

    def submit(self, key, path, body, kind, base_version_id=None):
        if kind != "train":
            return super().submit(key, path, body, kind, base_version_id)
        operation = self.state["operations"].get(key)
        if operation and operation.get("id"):
            return operation["id"]
        if operation:
            matches = self.recover(operation)
            if len(matches) != 1:
                raise RuntimeError(f"ambiguous submission {key}: {len(matches)} matches; POST was not repeated")
            operation.update(id=matches[0], status="submitted")
            self.save()
            self.log(f"recovered {key}: {matches[0]}")
            return matches[0]
        model = self.api.get("/investing/models/" + self.state["model_id"] + "?include_history=false")
        if (model["latest_version_id"] != base_version_id or model["session_count"] != len(self.state["rounds"])
                or model["active_job_id"] is not None):
            raise RuntimeError("stock model head or session count changed outside controller")
        operation = {"kind": kind, "path": path, "body": body, "base_version_id": base_version_id,
                     "before_count": model["session_count"], "status": "submitting"}
        self.state["operations"][key] = operation
        self.save()  # Intent is durable before the non-idempotent POST.
        result = self.api.post(path, body)
        operation.update(id=result["id"], status="submitted")
        self.save()  # Persist the receipt before polling.
        self.log(f"submitted {key}: {result['id']}")
        return result["id"]


class StockRewardTrial:
    def __init__(self, protocol, output, url, *, api=None):
        self.protocol = protocol
        self.requests = requests_from_protocol(protocol)
        self.output = Path(output).resolve()
        self.url = url.rstrip("/")
        self.api = api or API(self.url)
        self.runners = {}
        identity = digest({"protocol": protocol, "url": self.url})
        self.output.mkdir(parents=True, exist_ok=True)
        self.state_path = self.output / "state.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state["protocol_hash"] != identity:
                raise ValueError("stock protocol or URL changed; use a new output directory")
        else:
            self.state = {"protocol_hash": identity, "status": "initializing", "roles": {}}
            atomic_json(self.state_path, self.state)
        for role in ROLES:
            child_protocol = {"request": self.requests[role], "dataset_id": protocol["dataset_id"],
                              "workspace_path": protocol["workspace_path"]}
            self.runners[role] = StockRunner(child_protocol, self.output / role, self.url, api=self.api)

    def save(self):
        for role, runner in self.runners.items():
            rounds = runner.state["rounds"]
            self.state["roles"][role] = {"model_id": runner.state.get("model_id"),
                "successful_sessions": len(rounds), "decision_steps": len(rounds) * BUDGET,
                "latest_version_id": rounds[-1]["version_id"] if rounds else None,
                "last_job_id": runner.state.get("last_job_id")}
        atomic_json(self.state_path, self.state)

    def preflight(self):
        ensure_device(self.protocol["device"])
        dataset = self.api.get("/datasets/" + self.protocol["dataset_id"])
        if dataset.get("content_hash") != self.protocol["dataset_content_hash"]:
            raise RuntimeError("registered stock dataset hash differs from protocol")
        for role in ROLES:
            report = self.api.post("/capabilities/check", {"operation": "train", **self.requests[role]})
            if report.get("status") != "ready":
                raise RuntimeError(f"{role} stock training capability is blocked: {report.get('checks')}")

    def model(self, role, *, compact=False):
        runner = self.runners[role]
        path = "/investing/models/" + runner.state["model_id"]
        model = self.api.get(path + ("?include_history=false" if compact else ""))
        request = self.requests[role]
        spec = {**model["spec"], "backtest": dict(model["spec"]["backtest"])}
        symbols = spec.get("backtest", {}).pop("symbols", None)
        binding = model.get("framework_binding", {})
        refs = [entry.get("ref") for entry in binding.get("definitions", [])]
        if (model.get("id") != runner.state["model_id"] or model.get("dataset_id") != request["dataset_id"]
                or spec != request["spec"] or binding.get("basket_revision_ref") != request["basket_revision_ref"]
                or symbols != [entry["local_symbol"] for entry in binding.get("ordered_pairs", [])]
                or request["learning_definition_ref"] not in refs or request["trading_definition_ref"] not in refs):
            raise RuntimeError(f"{role} model or derived basket differs from frozen stock protocol")
        return model

    def check_manifest(self, role, manifest, previous, number, model):
        runner = self.runners[role]
        validate_manifest(manifest, runner.state["model_id"], previous["content_id"] if previous else None,
                          (number - 1) * BUDGET, BUDGET)
        if manifest.get("dataset_content_hash") != self.protocol["dataset_content_hash"]:
            raise RuntimeError("stock policy dataset content changed")
        spec = model["spec"]
        config = manifest.get("config", {})
        fixed = lambda value: {key: item for key, item in value.items() if key not in {"device", "total_timesteps"}}
        if fixed(config) != fixed(spec) or config.get("total_timesteps") != BUDGET:
            raise RuntimeError("stock policy configuration or session budget changed")
        if (config.get("device") != self.protocol["device"] or
                manifest.get("runtime", {}).get("device") != self.protocol["device"]):
            raise RuntimeError("stock policy device changed")
        from quanto.investing.contracts import PolicyTrainingSpec
        check_policy_identity(manifest, PolicyTrainingSpec.model_validate(spec))
        if manifest.get("metrics", {}).get("test") is not None:
            raise RuntimeError("training unexpectedly evaluated the test partition")
        if not {"policy.zip", "last.zip"}.issubset(manifest.get("files", {})):
            raise RuntimeError("stock policy checkpoints are incomplete")

    def verify_chain(self, role):
        runner = self.runners[role]
        model = self.model(role)
        sessions = {item["id"]: item for item in model["sessions"]}
        versions = {item["id"]: item["manifest"] for item in model["versions"]}
        previous = None
        rounds = runner.state["rounds"]
        if len(rounds) > ROUNDS:
            raise RuntimeError("stock training exceeded 600 successful sessions")
        for number, entry in enumerate(rounds, 1):
            base = previous["version_id"] if previous else None
            session = sessions.get(entry["version_id"], {})
            manifest = versions.get(entry["version_id"], {})
            if (entry.get("round") != number or entry.get("base_version_id") != base
                    or session.get("state") != "succeeded" or session.get("base_version_id") != base):
                raise RuntimeError("stock continuation session chain changed")
            self.check_manifest(role, manifest, previous, number, model)
            if (entry.get("content_id") != manifest["id"] or
                    any(entry.get(key) != manifest.get(key) for key in
                        ("timesteps_started", "timesteps_completed", "session_timesteps")) or
                    runner.state["operations"].get(f"train_{number:03d}", {}).get("id") != entry["version_id"]):
                raise RuntimeError("stock continuation receipt or checkpoint changed")
            saved = runner.output / f"round_{number:03d}.json"
            if not saved.is_file() or json.loads(saved.read_text(encoding="utf-8")) != manifest:
                raise RuntimeError("saved stock manifest changed")
            previous = entry
        pending = runner.state["operations"].get(f"train_{len(rounds) + 1:03d}")
        pending_id = None
        if pending:
            matches = [pending["id"]] if pending.get("id") else runner.recover(pending)
            if len(matches) != 1:
                raise RuntimeError("ambiguous pending stock session; POST was not repeated")
            pending_id = matches[0]
            if sessions.get(pending_id, {}).get("base_version_id") != (previous["version_id"] if previous else None):
                raise RuntimeError("pending stock session parent changed")
        known = {entry["version_id"] for entry in rounds} | ({pending_id} if pending_id else set())
        if (set(sessions) != known or model.get("latest_version_id") not in
                {previous["version_id"] if previous else None, pending_id}
                or model.get("active_job_id") not in {None, pending_id}):
            raise RuntimeError("stock model sessions or head changed outside controller")
        if previous:
            local = read_manifest(Path(runner.db_row("learning_artifacts", previous["version_id"])["path"]), "policy")
            if local != versions[previous["version_id"]]:
                raise RuntimeError("stock parent checkpoint differs from API artifact")
        return model

    def train_round(self, role, number):
        runner = self.runners[role]
        if len(runner.state["rounds"]) >= number:
            return
        previous = runner.state["rounds"][-1] if runner.state["rounds"] else None
        base = previous["version_id"] if previous else None
        job_id = runner.submit(f"train_{number:03d}", f"/investing/models/{runner.state['model_id']}/train",
                               {"additional_timesteps": BUDGET, "device": self.protocol["device"]}, "train", base)
        runner.state["last_job_id"] = job_id
        runner.save()
        self.save()
        runner.wait(job_id)
        model = self.model(role, compact=True)
        page = self.api.get(f"/investing/models/{runner.state['model_id']}/history"
                            f"?kind=sessions&session_number={number}&page_size=1")
        session = next((item for item in page["items"] if item["id"] == job_id), None)
        if (session is None or session["state"] != "succeeded" or session["base_version_id"] != base
                or model["latest_version_id"] != job_id):
            raise RuntimeError("stock session did not succeed at expected parent/head")
        manifest = self.api.get("/investing/policies/" + job_id)["manifest"]
        self.check_manifest(role, manifest, previous, number, model)
        local = read_manifest(Path(runner.db_row("learning_artifacts", job_id)["path"]), "policy")
        if local != manifest:
            raise RuntimeError("stock checkpoint differs from API artifact")
        atomic_json(runner.output / f"round_{number:03d}.json", manifest)
        runner.state["rounds"].append({"round": number, "version_id": job_id,
            "base_version_id": base, "content_id": manifest["id"],
            "timesteps_started": manifest["timesteps_started"],
            "timesteps_completed": manifest["timesteps_completed"],
            "session_timesteps": manifest["session_timesteps"]})
        runner.save()
        self.save()
        runner.log(f"stock {role} session {number}/{ROUNDS}: {number * BUDGET} decision steps")

    def run(self):
        try:
            self.preflight()
            for role, runner in self.runners.items():
                runner.state["dataset_id"] = self.protocol["dataset_id"]
                runner.save()
                runner.dataset()
                if runner.dataset_hash != self.protocol["dataset_content_hash"]:
                    raise RuntimeError("registered stock dataset differs from frozen protocol")
                if not runner.state.get("model_id"):
                    runner.state["model_id"] = runner.submit("model", "/investing/models", self.requests[role], "model")
                    runner.save()
                if runner.state["operations"]["model"].get("id") != runner.state["model_id"]:
                    raise RuntimeError("stock model ID differs from creation receipt")
                self.verify_chain(role)
                self.save()
            if self.runners["control"].state["model_id"] == self.runners["dsr"].state["model_id"]:
                raise RuntimeError("stock reward trial requires two new distinct models")
            counts = [len(self.runners[role].state["rounds"]) for role in ROLES]
            if counts[0] < counts[1] or counts[0] > counts[1] + 1:
                raise RuntimeError("stock reward chains violate alternating order")
            self.state["status"] = "training"
            self.save()
            for number in range(1, ROUNDS + 1):
                for role in ROLES:
                    self.train_round(role, number)
            if any(len(runner.state["rounds"]) != ROUNDS for runner in self.runners.values()):
                raise RuntimeError("both stock methods must finish exactly 600 sessions")
            self.state["status"] = "complete"
            self.save()
        except (Exception, KeyboardInterrupt) as exc:
            self.state["status"] = "stopped"
            self.state["stop"] = {"error_type": type(exc).__name__}
            self.save()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    protocol = json.loads(args.protocol.read_text(encoding="utf-8-sig"))
    requests = requests_from_protocol(protocol)
    if args.validate_only:
        ensure_device(protocol["device"])
        api = API(args.url)
        dataset = api.get("/datasets/" + protocol["dataset_id"])
        if dataset.get("content_hash") != protocol["dataset_content_hash"]:
            raise RuntimeError("registered stock dataset hash differs from protocol")
        for role in ROLES:
            report = api.post("/capabilities/check", {"operation": "train", **requests[role]})
            if report.get("status") != "ready":
                raise RuntimeError(f"{role} stock training capability is blocked: {report.get('checks')}")
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "controller.log").open("a", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            try:
                with controller_lock(args.output):
                    StockRewardTrial(protocol, args.output, args.url).run()
            except (Exception, KeyboardInterrupt) as exc:
                print(f"stopped: {type(exc).__name__}: {exc}; inspect state.json and receipts", flush=True)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
