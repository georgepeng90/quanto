"""Recovery behavior for the dedicated runner; never contacts a live API."""
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).parents[1] / "scripts/run_fixed_basket_training.py"
    assert path.exists(), "fixed-basket runner has not been implemented"
    spec = importlib.util.spec_from_file_location("fixed_basket_runner", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class FakeAPI:
    def __init__(self):
        self.posts = 0
        self.sessions = []
        self.job_state = "succeeded"

    def get(self, path):
        if path == "/investing/models/model":
            return {"sessions": self.sessions}
        if path.startswith("/jobs/"):
            return {"id": "job", "state": self.job_state, "error": "worker failed"}
        if path.startswith("/jobs?"):
            return {"items": [], "next_cursor": None}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts += 1
        self.sessions.append({"id": "job", "base_version_id": None})
        raise TimeoutError("response lost after server accepted request")


def runner(tmp_path, api):
    protocol = {"dataset_path": str(tmp_path), "spec": {"name": "test"},
                "rounds": 50, "timesteps_per_round": 512,
                "monitor_period": {"start": "2025-01-01", "end": "2025-12-31"},
                "final_period": {"start": "2026-01-01", "end": "2026-09-30"}}
    return module().Runner(protocol, tmp_path / "out", "http://127.0.0.1:8770", api=api)


def test_lost_response_recovers_session_without_duplicate_training(tmp_path):
    api = FakeAPI()
    first = runner(tmp_path, api)
    first.state["model_id"] = "model"
    first.save()
    with pytest.raises(TimeoutError):
        first.submit("train_001", "/investing/models/model/train",
                     {"additional_timesteps": 512, "device": "cpu"}, "train", None)
    saved = json.loads((tmp_path / "out/state.json").read_text())
    assert saved["operations"]["train_001"]["status"] == "submitting"
    resumed = runner(tmp_path, api)
    assert resumed.submit("train_001", "/investing/models/model/train",
                          {"additional_timesteps": 512, "device": "cpu"}, "train", None) == "job"
    assert api.posts == 1


def test_failed_job_stops_and_remains_recorded(tmp_path):
    api = FakeAPI()
    api.job_state = "failed"
    current = runner(tmp_path, api)
    current.state["operations"]["train_001"] = {"id": "job", "status": "submitted"}
    current.save()
    with pytest.raises(RuntimeError, match="failed"):
        current.wait("job")
    assert current.state["rounds"] == []
    resumed = runner(tmp_path, api)
    assert resumed.submit("train_001", "unused", {}, "train", None) == "job"
    assert api.posts == 0


def test_unknown_submission_without_a_match_cannot_be_resent(tmp_path):
    api = FakeAPI()
    current = runner(tmp_path, api)
    current.state["model_id"] = "model"
    current.state["operations"]["train_001"] = {
        "status": "submitting", "kind": "train", "base_version_id": None,
        "before_ids": [], "path": "/investing/models/model/train", "body": {}}
    current.save()
    with pytest.raises(RuntimeError, match="ambiguous"):
        current.submit("train_001", "unused", {}, "train", None)
    assert api.posts == 0


def test_chain_rejects_wrong_parent_and_nonincremental_timesteps():
    check = module().validate_manifest
    manifest = {"model_id": "m", "parent_content_id": "p", "timesteps_started": 512,
                "timesteps_completed": 1024, "session_timesteps": 512}
    check(manifest, "m", "p", 512, 512)
    with pytest.raises(ValueError):
        check(manifest, "m", "wrong", 512, 512)
    with pytest.raises(ValueError):
        check({**manifest, "timesteps_completed": 1025}, "m", "p", 512, 512)


def test_backtest_lost_response_matches_exact_frozen_request(tmp_path, monkeypatch):
    api = FakeAPI()
    current = runner(tmp_path, api)
    current.state["model_id"] = "model"
    body = {"dataset_id": "d", "version_id": "v", "spec": {
        "start": "2025-01-01", "end": "2025-12-31", "baseline": "equal_weight", "cost_multiplier": 1}}
    current.state["operations"]["monitor_equal_weight"] = {
        "status": "submitting", "kind": "backtest", "before_ids": [], "body": body}
    request = {"model_id": "model", "dataset_id": "d", "policy_id": "v", "spec": body["spec"]}
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps({"input": {"request": request}}))
    monkeypatch.setattr(current, "listing", lambda _: [{"id": "bt", "kind": "policy_backtest"}])
    monkeypatch.setattr(current, "db_row", lambda *_: {"input_path": str(path)})
    assert current.submit("monitor_equal_weight", "unused", {}, "backtest") == "bt"
    assert api.posts == 0


def test_completed_baseline_is_not_submitted_again(tmp_path):
    api = FakeAPI()
    current = runner(tmp_path, api)
    current.state["evaluations"]["monitor_equal_weight"] = {"job_id": "already_saved"}
    current.backtest("monitor_equal_weight", 20, "later_version", "monitor", {}, "equal_weight")
    assert api.posts == 0


def test_overlapping_monitor_and_final_rejected_before_dataset_import(tmp_path, monkeypatch):
    api = FakeAPI()
    current = runner(tmp_path, api)
    root = Path(__file__).parents[1]
    current.protocol["spec"] = json.loads((root / "examples/investing/weekly.json").read_text(encoding="utf-8"))
    current.protocol["monitor_period"] = {"start": "2025-01-01", "end": "2026-02-01"}
    monkeypatch.setattr(current, "dataset", lambda: pytest.fail("dataset import must not run"))
    with pytest.raises(ValueError, match="ordered and disjoint"):
        current.run()
    assert api.posts == 0


def extension_fixture(tmp_path):
    root = Path(__file__).parents[1]
    protocol = json.loads((root / "docs/research/2026-10-05-fixed-basket-training/protocol.json").read_text(encoding="utf-8"))
    from quanto.investing.contracts import PolicyTrainingSpec
    spec = PolicyTrainingSpec.model_validate(protocol["spec"]).model_dump(mode="json")
    rounds = [{"round": n, "version_id": f"v{n}", "content_id": f"c{n}",
               "base_version_id": f"v{n-1}" if n > 1 else None,
               "timesteps_started": (n-1)*512, "timesteps_completed": n*512, "session_timesteps": 512}
              for n in range(1, 51)]
    evaluations = {}
    for n in range(10, 51, 10):
        for snapshot in ("best", "last"):
            key = f"monitor_{n:03d}_{snapshot}"
            evaluations[key] = {"round": n, "version_id": f"v{n}", "period": "monitor",
                                "summary": {}, "snapshot": "last.zip_offline" if snapshot == "last" else "validation_best"}
    for period in ("monitor", "final"):
        for snapshot in (["best", "last", "equal_weight", "buy_hold"] if period == "final" else ["equal_weight", "buy_hold"]):
            evaluations[period + "_" + snapshot] = {"round": 50 if period == "final" else 10,
                "version_id": "v50" if period == "final" else "v10", "period": period, "summary": {}, "snapshot": snapshot}
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for key, entry in evaluations.items():
        (source_dir / (key + ".json")).write_text(json.dumps(entry["summary"]))
        if key.endswith("_last"):
            (source_dir / (key + ".equity.csv")).write_text("date,equity\n2025-01-03,1000000\n")
    source = {"complete": True, "model_id": "model", "dataset_id": "dataset", "protocol_hash": "original",
              "rounds": rounds, "evaluations": evaluations,
              "operations": {"model": {"body": {"dataset_id": "dataset", "spec": spec}}}}
    path = source_dir / "state.json"
    path.write_text(json.dumps(source))
    raw = path.read_bytes()
    protocol.update(rounds=250, source_state_path=str(path), source_state_sha256=hashlib.sha256(raw).hexdigest(), device_override="cuda")
    return protocol, path, raw, spec, source


def test_extension_preserves_source_and_reuses_identity_with_fresh_final_keys(tmp_path):
    protocol, path, raw, _, _ = extension_fixture(tmp_path)
    result = module().Runner(protocol, tmp_path / "extension", "http://127.0.0.1:8770", api=FakeAPI())
    assert len(result.state["rounds"]) == 50
    assert result.state["model_id"] == "model" and result.state["dataset_id"] == "dataset"
    assert result.state["complete"] is False
    assert "historical_final_050_best" in result.state["evaluations"]
    assert "final_250_best" not in result.state["evaluations"]
    assert result.state["evaluations"]["monitor_010_last"]["source_equity_path"] == str((path.parent / "monitor_010_last.equity.csv").resolve())
    assert result.final_prefix() == "final_250"
    assert path.read_bytes() == raw


def test_extension_rejects_unpinned_or_incomplete_source(tmp_path):
    protocol, path, raw, _, source = extension_fixture(tmp_path)
    protocol["source_state_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source state hash"):
        module().Runner(protocol, tmp_path / "extension", "http://127.0.0.1:8770", api=FakeAPI())
    source["complete"] = False
    path.write_text(json.dumps(source))
    protocol["source_state_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="completed 50"):
        module().Runner(protocol, tmp_path / "extension2", "http://127.0.0.1:8770", api=FakeAPI())


def test_extension_cuda_failure_occurs_before_dataset_or_training(tmp_path, monkeypatch):
    protocol, _, _, _, _ = extension_fixture(tmp_path)
    api = FakeAPI()
    mod = module()
    result = mod.Runner(protocol, tmp_path / "extension", "http://127.0.0.1:8770", api=api)
    def no_cuda(device):
        assert device == "cuda"
        raise RuntimeError("CUDA unavailable")
    monkeypatch.setattr(mod, "ensure_device", no_cuda)
    monkeypatch.setattr(result, "dataset", lambda: pytest.fail("dataset touched before CUDA check"))
    with pytest.raises(RuntimeError, match="CUDA unavailable"):
        result.run()
    assert api.posts == 0


def test_extension_round51_response_loss_recovers_without_reinitialization(tmp_path):
    protocol, path, raw, _, _ = extension_fixture(tmp_path)
    class ExtensionAPI(FakeAPI):
        def get(self, url):
            if url == "/investing/models/model":
                return {"sessions": self.sessions, "latest_version_id": "v50"}
            return super().get(url)
        def post(self, url, body):
            assert url == "/investing/models/model/train"
            assert body == {"additional_timesteps": 512, "device": "cuda"}
            self.posts += 1
            self.sessions.append({"id": "v51", "base_version_id": "v50"})
            raise TimeoutError("accepted then disconnected")
    api = ExtensionAPI()
    first = module().Runner(protocol, tmp_path / "extension", "http://127.0.0.1:8770", api=api)
    with pytest.raises(TimeoutError):
        first.submit("train_051", "/investing/models/model/train", {"additional_timesteps": 512, "device": "cuda"}, "train", "v50")
    resumed = module().Runner(protocol, tmp_path / "extension", "http://127.0.0.1:8770", api=api)
    assert resumed.submit("train_051", "unused", {}, "train", "v50") == "v51"
    assert api.posts == 1 and len(resumed.state["rounds"]) == 50
    assert path.read_bytes() == raw


def test_extension_rejects_source_changes_after_seed(tmp_path):
    protocol, path, raw, _, _ = extension_fixture(tmp_path)
    mod = module()
    mod.Runner(protocol, tmp_path / "extension", "http://127.0.0.1:8770", api=FakeAPI())
    path.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="source state hash"):
        mod.Runner(protocol, tmp_path / "extension", "http://127.0.0.1:8770", api=FakeAPI())

def stopped_extension_fixture(tmp_path):
    protocol, original_path, _, spec, _ = extension_fixture(tmp_path)
    mod = module()
    seeded = mod.Runner(protocol, tmp_path / "stopped", "http://127.0.0.1:8770", api=FakeAPI())
    source = seeded.state
    for n in range(51, 106):
        source["rounds"].append({"round": n, "version_id": f"v{n}", "content_id": f"c{n}",
            "base_version_id": f"v{n-1}", "timesteps_started": (n-1)*512,
            "timesteps_completed": n*512, "session_timesteps": 512})
    for n in range(60, 101, 10):
        for snapshot in ("best", "last"):
            key = f"monitor_{n:03d}_{snapshot}"
            source["evaluations"][key] = {"round": n, "version_id": f"v{n}", "period": "monitor",
                "summary": {}, "snapshot": snapshot}
            (seeded.output / (key + ".json")).write_text("{}")
            if snapshot == "last":
                (seeded.output / (key + ".equity.csv")).write_text("date,equity\n2025-01-03,1000000\n")
    source["stop"] = {"successful_rounds": 105, "active_job_id": None}
    seeded.save()
    raw = seeded.state_path.read_bytes()
    protocol.update(rounds=305, source_state_path=str(seeded.state_path),
                    source_state_sha256=hashlib.sha256(raw).hexdigest(), monitor_start_round=115)
    return protocol, seeded.state_path, raw, original_path, spec


def test_stopped105_extension_preserves_inherited_paths_and_does_not_rename_history(tmp_path):
    protocol, path, raw, original, _ = stopped_extension_fixture(tmp_path)
    current = module().Runner(protocol, tmp_path / "append200", "http://127.0.0.1:8770", api=FakeAPI())
    assert len(current.state["rounds"]) == 105
    assert current.state["extension"]["base_rounds"] == 105
    assert current.state["extension"]["base_version_id"] == "v105"
    assert current.state["evaluations"]["historical_final_050_last"]["source_summary_path"] == str(original.parent / "final_last.json")
    assert current.state["evaluations"]["monitor_010_last"]["source_equity_path"] == str(original.parent / "monitor_010_last.equity.csv")
    assert current.state["evaluations"]["monitor_100_last"]["source_summary_path"] == str(path.parent / "monitor_100_last.json")
    assert current.final_prefix() == "final_305"
    assert not any("historical_final_105_historical" in key for key in current.state["evaluations"])
    assert path.read_bytes() == raw


def test_stopped105_new_monitors_follow_append_offset_without_backfilling110(tmp_path, monkeypatch):
    protocol, _, _, _, spec = stopped_extension_fixture(tmp_path)
    mod = module()
    current = mod.Runner(protocol, tmp_path / "append200", "http://127.0.0.1:8770", api=FakeAPI())
    # Pretend all appended receipts are durable: exercise scheduling without any training.
    for n in range(106, 306):
        current.state["rounds"].append({"round": n, "version_id": f"v{n}"})
    monkeypatch.setattr(mod, "ensure_device", lambda _: None)
    monkeypatch.setattr(current, "dataset", lambda: "dataset")
    monkeypatch.setattr(current.api, "get", lambda _: {"spec": spec, "dataset_id": "dataset"})
    monkeypatch.setattr(current, "verify_extension", lambda *_: None)
    monkeypatch.setattr(current, "comparison", lambda: None)
    seen = []
    monkeypatch.setattr(current, "backtest", lambda key, number, *args: seen.append((key, number)))
    monkeypatch.setattr(current, "offline_last", lambda key, number, *args: seen.append((key, number)))
    current.run()
    assert [number for key, number in seen if key.startswith("monitor_") and key.endswith("_best")] == list(range(115, 306, 10))
    assert ("final_305_best", 305) in seen and ("final_305_last", 305) in seen
    assert current.api.posts == 0

@pytest.mark.parametrize("sampling", [None, {"algorithm": "base-seed-plus-parent-timesteps-v1", "base_seed": 42,
    "parent_timesteps": 53760, "effective_env_seed": 42}])
def test_append_rejects_old_worker_or_repeated_sampling_seed(tmp_path, monkeypatch, sampling):
    protocol, _, _, _, spec = stopped_extension_fixture(tmp_path)
    protocol["require_sampling_algorithm"] = "base-seed-plus-parent-timesteps-v1"
    mod = module()
    current = mod.Runner(protocol, tmp_path / "append200", "http://127.0.0.1:8770", api=FakeAPI())
    monkeypatch.setattr(mod, "ensure_device", lambda _: None)
    monkeypatch.setattr(current, "dataset", lambda: "dataset")
    monkeypatch.setattr(current, "verify_extension", lambda *_: None)
    manifest = {"model_id": "model", "parent_content_id": "c105", "timesteps_started": 53760,
        "timesteps_completed": 54272, "session_timesteps": 512, "config": {"device": "cuda"},
        "runtime": {"device": "cuda"}, "sampling_provenance": sampling}
    monkeypatch.setattr(current.api, "get", lambda path: {"manifest": manifest} if "/policies/" in path else {
        "spec": spec, "dataset_id": "dataset", "sessions": [{"id": "v106", "base_version_id": "v105"}],
        "latest_version_id": "v106"})
    def submit(key, path, body, kind, base):
        assert key == "train_106" and base == "v105"
        assert body == {"additional_timesteps": 512, "device": "cuda"}
        return "v106"
    monkeypatch.setattr(current, "submit", submit)
    monkeypatch.setattr(current, "wait", lambda _: None)
    with pytest.raises(RuntimeError, match="sampling algorithm"):
        current.run()
    assert len(current.state["rounds"]) == 105


def test_offline_last_uses_explicit_cuda_for_inference(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pandas as pd
    import torch
    from stable_baselines3 import PPO
    import quanto.backtest.dataset_loaders as loaders
    import quanto.investing.evaluation as evaluation
    import quanto.learning.artifacts as artifacts

    protocol, _, _, _, spec = stopped_extension_fixture(tmp_path)
    mod = module()
    current = mod.Runner(protocol, tmp_path / "append200", "http://127.0.0.1:8770", api=FakeAPI())
    current.dataset_hash, current.dataset_path = "hash", tmp_path / "dataset"
    monkeypatch.setattr(current, "db_row", lambda *_: {"path": str(tmp_path / "artifact")})
    monkeypatch.setattr(artifacts, "read_manifest", lambda *_: {
        "config": spec, "model_id": "model", "dataset_content_hash": "hash", "id": "content", "price_mode": "raw"})
    devices = []
    monkeypatch.setattr(mod, "ensure_device", lambda device: devices.append(device))
    def load(path, device):
        assert device == "cuda" and path.name == "last.zip"
        return SimpleNamespace(device=torch.device("cuda:0"))
    monkeypatch.setattr(PPO, "load", load)
    monkeypatch.setattr(loaders, "load_dataset", lambda _: None)
    monkeypatch.setattr(evaluation, "evaluate_policy", lambda *args: ({"sharpe": None},
        SimpleNamespace(equity=pd.DataFrame({"date": ["2025-01-03"], "equity": [1000000]})),
        SimpleNamespace(metrics={}, metric_definitions={}, warnings=[])))
    monkeypatch.setattr(current, "comparison", lambda: None)
    current.offline_last("monitor_115_last", 115, "v115", "monitor", protocol["monitor_period"])
    assert devices == ["cuda"]
    assert current.state["evaluations"]["monitor_115_last"]["summary"]["runtime"]["device"] == "cuda:0"

@pytest.mark.parametrize("change", ["active", "parent", "missing_monitor"])
def test_stopped105_rejects_unsafe_source_before_any_post(tmp_path, change):
    protocol, path, _, _, _ = stopped_extension_fixture(tmp_path)
    source = json.loads(path.read_text(encoding="utf-8"))
    if change == "active":
        source["stop"]["active_job_id"] = "running"
    elif change == "parent":
        source["rounds"][-1]["base_version_id"] = "wrong"
    else:
        source["evaluations"].pop("monitor_100_last")
    path.write_text(json.dumps(source))
    protocol["source_state_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    api = FakeAPI()
    with pytest.raises(ValueError):
        module().Runner(protocol, tmp_path / "append200", "http://127.0.0.1:8770", api=api)
    assert api.posts == 0
