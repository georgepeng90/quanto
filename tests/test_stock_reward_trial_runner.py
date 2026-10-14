"""The stock trial counts only verified successes and never repeats uncertain POSTs."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import policy_identity
from quanto.learning.artifacts import write_manifest


def module():
    path = Path(__file__).parents[1] / "scripts/run_stock_reward_trial.py"
    spec = importlib.util.spec_from_file_location("stock_reward_trial", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class FakeAPI:
    def __init__(self, root):
        self.root = root
        self.models = {}
        self.manifests = {}
        self.posts = []
        self.lose_response_at = None
        self.fail_at = None
        self.block_role = None

    def get(self, path):
        if path == "/datasets/dataset":
            return {"content_hash": "a" * 64}
        if path.startswith("/investing/models?"):
            return {"items": copy.deepcopy(list(self.models.values())), "next_cursor": None}
        if "/history?" in path:
            identity = path.split("/")[3]
            number = int(path.split("session_number=")[1].split("&")[0])
            sessions = self.models[identity]["sessions"]
            return {"items": copy.deepcopy([sessions[number - 1]]) if number <= len(sessions) else []}
        if path.startswith("/investing/models/"):
            identity = path.split("/")[3].split("?")[0]
            model = copy.deepcopy(self.models[identity])
            model["session_count"] = len(model["sessions"])
            if "?include_history=false" in path:
                del model["sessions"], model["versions"]
            return model
        if path.startswith("/investing/policies/"):
            return {"manifest": copy.deepcopy(self.manifests[path.rsplit("/", 1)[1]])}
        if path.startswith("/jobs/"):
            identity = path.rsplit("/", 1)[1]
            return {"id": identity, "state": "failed" if identity == self.fail_at else "succeeded"}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, copy.deepcopy(body)))
        if path == "/capabilities/check":
            return {"status": "blocked" if body["spec"]["name"] == self.block_role else "ready"}
        if path == "/investing/models":
            identity = f"model{len(self.models) + 1}"
            spec = copy.deepcopy(body["spec"])
            spec["backtest"]["symbols"] = ["SZSE:stock:000001"]
            self.models[identity] = {"id": identity, "dataset_id": body["dataset_id"], "spec": spec,
                "framework_binding": {"basket_revision_ref": body["basket_revision_ref"],
                    "ordered_pairs": [{"local_symbol": "SZSE:stock:000001"}],
                    "definitions": [{"ref": body["learning_definition_ref"]},
                                    {"ref": body["trading_definition_ref"]}]},
                "sessions": [], "versions": [], "latest_version_id": None, "active_job_id": None}
        else:
            assert path.endswith("/train")
            assert len(self.models) == 2
            model = self.models[path.split("/")[3]]
            number = len(model["sessions"]) + 1
            identity = f"{model['id']}-{number}"
            base = model["latest_version_id"]
            failed = identity == self.fail_at
            model["sessions"].append({"id": identity, "base_version_id": base,
                                       "state": "failed" if failed else "succeeded"})
            if not failed:
                spec = model["spec"]
                config = {**spec, "total_timesteps": 1024}
                start = (number - 1) * 1024
                manifest = {"kind": "policy", "model_id": model["id"], "config": config,
                    "parent_content_id": self.manifests[base]["id"] if base else None,
                    "dataset_content_hash": "a" * 64, "basket": spec["backtest"]["symbols"],
                    "timesteps_started": start, "timesteps_completed": start + 1024,
                    "session_timesteps": 1024, "runtime": {"device": "cpu"},
                    "metrics": {"test": None}, **policy_identity(PolicyTrainingSpec.model_validate(config))}
                directory = self.root / identity
                directory.mkdir()
                (directory / "policy.zip").write_bytes(b"policy")
                (directory / "last.zip").write_bytes(b"last")
                manifest = write_manifest(directory, manifest)
                self.manifests[identity] = manifest
                model["versions"].append({"id": identity, "manifest": manifest})
                model["latest_version_id"] = identity
        if identity == self.lose_response_at:
            self.lose_response_at = None
            raise TimeoutError("response lost after accepted POST")
        return {"id": identity}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    mod = module()
    monkeypatch.setattr(mod, "ROUNDS", 2)
    raw = json.loads((Path(__file__).parents[1] / "examples/investing/weekly.json").read_text(encoding="utf-8"))
    raw["backtest"].pop("symbols")
    raw.update(device="cpu", n_steps=128, total_timesteps=1024)
    models = {}
    for role, (definition, reward) in mod.REWARDS.items():
        models[role] = {"learning_definition_ref": {"kind": "learning", "id": definition, "version": "1"},
                        "spec": {**raw, "name": role, "reward_version": reward}}
    protocol = {"dataset_id": "dataset", "dataset_content_hash": "a" * 64,
        "workspace_path": str(tmp_path), "basket_revision_ref": {"basket_id": "basket", "revision": 1},
        "framework_contract_version": "multi-asset-v1",
        "trading_definition_ref": {"kind": "trading", "id": "stock-daily", "version": "1"},
        "models": models, "rounds": 2, "timesteps_per_round": 1024, "device": "cpu"}
    api = FakeAPI(tmp_path)
    monkeypatch.setattr(mod.StockRunner, "log", lambda *_: None)
    monkeypatch.setattr(mod.StockRunner, "db_row", lambda _, table, identity:
        {"content_hash": "a" * 64, "path": str(tmp_path if table == "datasets" else tmp_path / identity)})
    return mod, protocol, api, tmp_path / "out"


def test_stock_protocol_rejects_wrong_budget_and_legacy_definition(setup):
    mod, protocol, _, _ = setup
    protocol["timesteps_per_round"] = 512
    with pytest.raises(ValueError, match="1024"):
        mod.requests_from_protocol(protocol)
    protocol["timesteps_per_round"] = 1024
    protocol["models"]["control"]["learning_definition_ref"]["id"] = "etf-ppo"
    with pytest.raises(ValueError, match="wrong stock learning definition"):
        mod.requests_from_protocol(protocol)


def test_protocol_accepts_held_out_test_partition_but_requires_session_budget(setup):
    mod, protocol, _, _ = setup
    requests = mod.requests_from_protocol(protocol)
    assert requests["control"]["spec"]["test_start"] == "2025-01-01"
    assert requests["dsr"]["spec"]["test_end"] == "2026-09-30"
    protocol["models"]["control"]["spec"]["total_timesteps"] = 512
    with pytest.raises(ValueError, match="fixed stock reward protocol"):
        mod.requests_from_protocol(protocol)


def test_protocol_fixes_600_sessions_and_preflights_both_before_creation(setup, monkeypatch):
    mod, protocol, api, output = setup
    monkeypatch.setattr(mod, "ROUNDS", 600)
    protocol["rounds"] = 599
    with pytest.raises(ValueError, match="600"):
        mod.requests_from_protocol(protocol)
    protocol["rounds"] = 600
    api.block_role = "dsr"
    with pytest.raises(RuntimeError, match="blocked"):
        mod.StockRewardTrial(protocol, output, "http://fake", api=api).run()
    assert [path for path, _ in api.posts] == ["/capabilities/check", "/capabilities/check"]


def test_two_models_alternate_and_complete_only_verified_sessions(setup):
    mod, protocol, api, output = setup
    controller = mod.StockRewardTrial(protocol, output, "http://fake", api=api)
    controller.run()
    assert controller.state["status"] == "complete"
    assert [controller.state["roles"][role]["decision_steps"] for role in mod.ROLES] == [2048, 2048]
    assert [path for path, _ in api.posts if path.endswith("/train")] == [
        "/investing/models/model1/train", "/investing/models/model2/train",
        "/investing/models/model1/train", "/investing/models/model2/train"]
    assert len([path for path, _ in api.posts if path == "/capabilities/check"]) == 2
    mod.StockRewardTrial(protocol, output, "http://fake", api=api).run()
    assert len([path for path, _ in api.posts if path.endswith("/train")]) == 4


def test_ambiguous_response_recovers_once_without_reposting(setup):
    mod, protocol, api, output = setup
    api.lose_response_at = "model1-1"
    with pytest.raises(TimeoutError):
        mod.StockRewardTrial(protocol, output, "http://fake", api=api).run()
    api.fail_at = "model2-1"
    with pytest.raises(RuntimeError, match="failed"):
        mod.StockRewardTrial(protocol, output, "http://fake", api=api).run()
    assert len([path for path, _ in api.posts if path == "/investing/models/model1/train"]) == 1
    control = json.loads((output / "control/state.json").read_text(encoding="utf-8"))
    dsr = json.loads((output / "dsr/state.json").read_text(encoding="utf-8"))
    assert len(control["rounds"]) == 1 and dsr["rounds"] == []
    posts = len(api.posts)
    with pytest.raises(RuntimeError, match="failed"):
        mod.StockRewardTrial(protocol, output, "http://fake", api=api).run()
    assert len(api.posts) == posts + 2  # Read-only capability checks only.


def test_unknown_submission_without_unique_match_stops(setup):
    mod, protocol, api, output = setup
    api.lose_response_at = "model1-1"
    with pytest.raises(TimeoutError):
        mod.StockRewardTrial(protocol, output, "http://fake", api=api).run()
    api.models["model1"]["sessions"].clear()
    api.models["model1"]["versions"].clear()
    api.models["model1"]["latest_version_id"] = None
    before = len([path for path, _ in api.posts if path.endswith("/train")])
    with pytest.raises(RuntimeError, match="ambiguous"):
        mod.StockRewardTrial(protocol, output, "http://fake", api=api).run()
    assert len([path for path, _ in api.posts if path.endswith("/train")]) == before
