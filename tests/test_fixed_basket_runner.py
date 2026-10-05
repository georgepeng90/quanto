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
