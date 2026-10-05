"""Exercise paired orchestration with durable real receipts and a local fake API."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import policy_identity
from quanto.learning.artifacts import write_manifest


def module():
    path = Path(__file__).parents[1] / "scripts/run_reward_trial.py"
    assert path.exists(), "paired reward runner is not implemented"
    spec = importlib.util.spec_from_file_location("reward_trial_runner", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_dedicated_controller_is_available():
    assert callable(module().RewardTrialRunner)


class FakeAPI:
    def __init__(self, directory):
        self.directory = directory
        self.models = {}
        self.manifests = {}
        self.posts = []
        self.lose_response_at = None
        self.fail_at = None
        self.corrupt = None

    def get(self, path):
        if path.startswith("/investing/models?"):
            return {"items": list(self.models.values()), "next_cursor": None}
        if path.startswith("/investing/models/"):
            return copy.deepcopy(self.models[path.rsplit("/", 1)[1]])
        if path.startswith("/investing/policies/"):
            return {"manifest": copy.deepcopy(self.manifests[path.rsplit("/", 1)[1]])}
        if path == "/datasets/dataset":
            return {"id": "dataset", "content_hash": "dataset-hash"}
        if path.startswith("/jobs/"):
            identity = path.rsplit("/", 1)[1]
            return {"id": identity, "state": "failed" if identity == self.fail_at else "succeeded"}
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, copy.deepcopy(body)))
        if path == "/investing/models":
            identity = f"model{len(self.models) + 1}"
            self.models[identity] = dict(id=identity, **copy.deepcopy(body), sessions=[], versions=[],
                                         latest_version_id=None, active_job_id=None)
        else:
            assert path.endswith("/train"), "no monitoring backtests are permitted"
            assert len(self.models) == 2, "both fresh models must exist before training"
            model = self.models[path.split("/")[3]]
            number = len(model["sessions"]) + 1
            identity = f"{model['id']}-{number}"
            base = model["latest_version_id"]
            model["sessions"].append(dict(id=identity, base_version_id=base,
                                           state="failed" if identity == self.fail_at else "succeeded"))
            if identity != self.fail_at:
                config = {**model["spec"], "device": "cuda", "total_timesteps": 512}
                start = (number - 1) * 512
                manifest = dict(kind="policy", config=config, model_id=model["id"],
                    parent_content_id=self.manifests[base]["id"] if base else None,
                    dataset_content_hash="dataset-hash", basket=config["backtest"]["symbols"],
                    timesteps_started=start, timesteps_completed=number * 512, session_timesteps=512,
                    runtime={"device": "cuda"}, selection_metric="validation_sharpe",
                    metrics={"test": None, "validation": {"sharpe": 1}},
                    sampling_provenance=dict(algorithm="base-seed-plus-parent-timesteps-v1",
                        base_seed=config["seed"], parent_timesteps=start,
                        effective_env_seed=config["seed"] + start),
                    **policy_identity(PolicyTrainingSpec.model_validate(config)))
                if self.corrupt:
                    self.corrupt(manifest)
                directory = self.directory / identity
                directory.mkdir()
                (directory / "policy.zip").write_bytes(b"fake-selected-policy")
                (directory / "last.zip").write_bytes(b"fake-latest-policy")
                manifest = write_manifest(directory, manifest)
                self.manifests[identity] = manifest
                model["versions"].append(dict(id=identity, manifest=manifest))
                model["latest_version_id"] = identity
        if identity == self.lose_response_at:
            self.lose_response_at = None
            raise TimeoutError("response lost after acceptance")
        return {"id": identity}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    runner_module = module()
    raw = json.loads((Path(__file__).parents[1] / "examples/investing/weekly.json").read_text(encoding="utf-8"))
    raw.update(test_start=None, test_end=None, selection_metric="validation_sharpe", gamma=1)
    raw["backtest"]["symbols"] = [str(500000 + n) for n in range(32)]
    specs = {}
    for role, reward in (("control", "net-log-return-with-state-v1"), ("dsr", "differential-sharpe-v1")):
        specs[role] = PolicyTrainingSpec.model_validate({**raw, "name": role, "reward_version": reward}).model_dump(mode="json")
    trial = dict(specs=specs, dataset_content_hash="dataset-hash", dataset_path=str(tmp_path),
                 validation_start="2024-01-01", validation_end="2024-12-31")
    trial_path = tmp_path / "trial.json"
    trial_path.write_text(json.dumps(trial), encoding="utf-8")
    config = dict(trial_path=str(trial_path), dataset_id="dataset", workspace_path=str(tmp_path),
                  rounds=300, timesteps_per_round=512, url="http://fake")
    api = FakeAPI(tmp_path)
    monkeypatch.setattr(runner_module, "ensure_device", lambda _: None)
    monkeypatch.setattr(runner_module.Runner, "log", lambda *_: None)

    def db_row(_, table, identity):
        if table == "datasets":
            return dict(path=str(tmp_path), content_hash="dataset-hash")
        return dict(path=str(tmp_path / identity))

    monkeypatch.setattr(runner_module.Runner, "db_row", db_row)
    comparisons = []

    def compare(trial_path, control, dsr, output):
        assert len(api.models["model1"]["sessions"]) == len(api.models["model2"]["sessions"]) == 300
        comparisons.append((control, dsr))
        output.mkdir()
        result = dict(kind="policy_reward_comparison", dataset_content_hash="dataset-hash",
            validation_period=dict(start="2024-01-01", end="2024-12-31"),
            training=dict(timesteps_completed=153600, session_timesteps=512), results={})
        for role, policy_dir in (("control", control), ("dsr", dsr)):
            manifest = api.manifests[policy_dir.name]
            result["results"][role] = dict(model_id=manifest["model_id"], policy_content_id=manifest["id"],
                                          timesteps_completed=153600, session_timesteps=512)
            directory = output / role
            directory.mkdir()
            (directory / "summary.json").write_text(json.dumps(dict(kind="policy_validation",
                evaluation_purpose="model_selection", policy_content_id=manifest["id"],
                training_dataset_content_hash="dataset-hash", period=result["validation_period"])))
            (directory / "equity.csv").write_text("date,equity\n2024-01-01,1\n")
        (output / "comparison.json").write_text(json.dumps(result))
        return result

    monkeypatch.setattr(runner_module, "compare_trial", compare)
    return runner_module, config, api, tmp_path / "output", comparisons


def run(setup):
    mod, config, api, output, _ = setup
    return mod.RewardTrialRunner(config, output, api=api)


def test_alternates_exactly_300_successful_rounds_then_compares_once(setup):
    controller = run(setup)
    controller.run()
    _, _, api, output, comparisons = setup
    training = [path.split("/")[3] for path, _ in api.posts if path.endswith("/train")]
    assert training == ["model1", "model2"] * 300
    assert len(comparisons) == 1
    assert json.loads((output / "state.json").read_text())["status"] == "complete"
    run(setup).run()
    assert len(api.posts) == 602
    assert len(comparisons) == 1


@pytest.mark.parametrize("lost", ["model1", "model2", "model1-1", "model2-1"])
def test_response_loss_stops_then_resumes_without_duplicate_post(setup, lost):
    _, _, api, output, comparisons = setup
    api.lose_response_at = lost
    with pytest.raises(TimeoutError):
        run(setup).run()
    assert comparisons == []
    assert json.loads((output / "state.json").read_text())["status"] == "stopped"
    # Stop soon after recovery to test the receipt without running 600 rounds again.
    api.fail_at = "model1-2"
    with pytest.raises(RuntimeError, match="failed"):
        run(setup).run()
    assert sum(path.endswith("/train") for path, _ in api.posts) == 3
    assert len(api.models) == 2
    assert len(api.models["model1"]["sessions"]) == 2
    assert comparisons == []


@pytest.mark.parametrize("change", ["trial_bytes", "config"])
def test_changed_trial_or_configuration_refuses_resume_before_post(setup, change):
    _, config, api, _, _ = setup
    api.fail_at = "model1-1"
    with pytest.raises(RuntimeError):
        run(setup).run()
    posts = len(api.posts)
    if change == "trial_bytes":
        path = Path(config["trial_path"])
        path.write_text(path.read_text() + "\n")
    else:
        config["source_note"] = "changed provenance"
    with pytest.raises(ValueError, match="changed"):
        run(setup)
    assert len(api.posts) == posts


def test_failed_job_remains_recorded_without_compare_or_retry(setup):
    _, _, api, output, comparisons = setup
    api.fail_at = "model2-1"
    with pytest.raises(RuntimeError, match="failed"):
        run(setup).run()
    posts = len(api.posts)
    with pytest.raises(RuntimeError, match="failed"):
        run(setup).run()
    assert len(api.posts) == posts
    assert comparisons == []
    state = json.loads((output / "dsr/state.json").read_text())
    assert state["rounds"] == []
    assert state["last_failure"]["job_id"] == "model2-1"


def test_external_head_advance_refuses_resume(setup):
    _, _, api, _, comparisons = setup
    api.lose_response_at = "model2-1"
    with pytest.raises(TimeoutError):
        run(setup).run()
    api.models["model1"]["latest_version_id"] = "external"
    posts = len(api.posts)
    with pytest.raises(RuntimeError, match="outside"):
        run(setup).run()
    assert len(api.posts) == posts
    assert comparisons == []


@pytest.mark.parametrize("field", ["dataset_content_hash", "reward_version", "sampling_provenance", "runtime", "session_timesteps"])
def test_invalid_completed_manifest_cannot_count_as_success(setup, field):
    _, _, api, output, comparisons = setup
    api.corrupt = lambda manifest: manifest.__setitem__(field, "wrong")
    with pytest.raises((ValueError, RuntimeError)):
        run(setup).run()
    assert json.loads((output / "control/state.json").read_text())["rounds"] == []
    assert comparisons == []


def test_unknown_post_cannot_be_repeated_without_exactly_one_recovery_match(setup):
    _, _, api, _, comparisons = setup
    api.lose_response_at = "model1-1"
    with pytest.raises(TimeoutError):
        run(setup).run()
    api.models["model1"]["sessions"] = []
    api.models["model1"]["versions"] = []
    api.models["model1"]["latest_version_id"] = None
    posts = len(api.posts)
    with pytest.raises(RuntimeError, match="ambiguous"):
        run(setup).run()
    assert len(api.posts) == posts
    assert comparisons == []


def test_final_report_saved_before_completion_is_recovered_without_evaluation(setup, monkeypatch):
    mod, _, api, output, comparisons = setup
    controller = run(setup)
    original = controller.verify_final

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(controller, "verify_final", interrupted)
    with pytest.raises(KeyboardInterrupt):
        controller.run()
    assert (output / "final/comparison.json").is_file()
    assert len(comparisons) == 1
    monkeypatch.setattr(controller, "verify_final", original)
    run(setup).run()
    assert len(api.posts) == 602
    assert len(comparisons) == 1
    # A previously started evaluation without a complete saved report is uncertain.
    state = json.loads((output / "state.json").read_text())
    state["status"] = "stopped"
    mod.atomic_json(output / "state.json", state)
    (output / "final/comparison.json").unlink()
    with pytest.raises(RuntimeError, match="evaluation not repeated"):
        run(setup).run()
    assert len(comparisons) == 1
    assert len(api.posts) == 602


def test_os_lock_blocks_second_controller_and_releases_on_exit(tmp_path):
    mod = module()
    with mod.controller_lock(tmp_path):
        with pytest.raises(OSError):
            with mod.controller_lock(tmp_path):
                pytest.fail("second controller acquired the same lock")
    with mod.controller_lock(tmp_path):
        pass
