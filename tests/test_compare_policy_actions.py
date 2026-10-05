"""Dry-run action comparison checks; no API or live workspace access."""

import importlib.util
import json
from pathlib import Path

import pytest

from tests.fixtures.investing_training_cases import investing_case


def comparison_module():
    path = Path(__file__).parents[1] / "scripts/compare_policy_actions.py"
    spec = importlib.util.spec_from_file_location("compare_policy_actions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def protocol_file(tmp_path):
    spec, _ = investing_case(tmp_path / "dataset")
    from quanto.backtest.dataset_loaders import load_dataset
    from quanto.backtest.dataset_storage import write_parquet_dataset
    from quanto.learning.registry import hash_files
    from quanto.objects import digest, json_bytes

    registered = tmp_path / "registered"
    write_parquet_dataset(load_dataset(tmp_path / "dataset"), registered)
    content_hash = digest(json_bytes(hash_files(registered)))
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps({"dataset_path": str(registered), "spec": spec}), encoding="utf-8")
    return path, content_hash


def prepared(tmp_path):
    module = comparison_module()
    protocol, content_hash = protocol_file(tmp_path)
    matrix = module.prepare_matrix(protocol, "registered-dataset", content_hash, tmp_path / "matrix",
                                   budget=16, horizons=(10, 20), seeds=(0, 42))
    return module, matrix


def manifest_for(case, matrix, *, score=1.):
    return {"kind": "policy", "config": case["spec"], "model_id": "model-" + case["id"],
            "action_version": case["action_version"], "dataset_content_hash": matrix["dataset_content_hash"],
            "feature_version": "daily-etf-v3", "environment_version": "daily-session-v1",
            "execution_version": "next-open-ledger-v1", "price_mode": "raw_cash",
            "runtime": {"python": "3", "torch": "2", "stable_baselines3": "2.9", "gymnasium": "1",
                        "numpy": "2", "cloudpickle": "3", "device": "cpu", "cuda_version": None,
                        "device_name": "CPU", "deterministic_algorithms": True},
            "timesteps_started": 0, "timesteps_completed": matrix["budget"],
            "session_timesteps": matrix["budget"], "selection_valid": True,
            "metrics": {"validation": {"sharpe": score, "net_log_return": score / 10,
                                       "max_drawdown": .2}, "test": None}}


def exported_manifests(tmp_path, matrix):
    directory = tmp_path / "exports"
    directory.mkdir()
    for case in matrix["cases"]:
        (directory / f"{case['id']}.json").write_text(json.dumps({"manifest": manifest_for(case, matrix)}), encoding="utf-8")
    return directory


def test_prepare_writes_complete_dry_run_matrix_without_training(tmp_path):
    _, matrix = prepared(tmp_path)
    assert len(matrix["cases"]) == 8
    assert {case["seed"] for case in matrix["cases"]} == {0, 42}
    assert {case["action_version"] for case in matrix["cases"]} == {
        "hold-target-quartiles-v1", "hold-target-cash-quartiles-v2"}
    assert matrix["train_sessions"] == 30
    for case in matrix["cases"]:
        folder = tmp_path / "matrix" / case["id"]
        assert json.loads((folder / "create-model.json").read_text())["spec"] == case["spec"]
        assert json.loads((folder / "train-model.json").read_text()) == {"additional_timesteps": 16,
                                                                         "device": case["spec"]["device"]}


def test_prepare_rejects_unavailable_504_day_window_without_output(tmp_path):
    module = comparison_module()
    protocol, content_hash = protocol_file(tmp_path)
    output = tmp_path / "matrix"
    with pytest.raises(ValueError, match="504|episode_sessions|training interval"):
        module.prepare_matrix(protocol, "registered-dataset", content_hash, output,
                              budget=16, horizons=(10, 20, 504), seeds=(17, 42))
    assert not output.exists()


def test_prepare_rejects_budget_that_overshoots_rollout(tmp_path):
    module = comparison_module()
    protocol, content_hash = protocol_file(tmp_path)
    with pytest.raises(ValueError, match="budget|n_steps"):
        module.prepare_matrix(protocol, "registered-dataset", content_hash, tmp_path / "matrix",
                              budget=17, horizons=(10, 20), seeds=(17, 42))


def test_prepare_rejects_dataset_path_that_differs_from_registered_hash(tmp_path):
    module = comparison_module()
    protocol, _ = protocol_file(tmp_path)
    with pytest.raises(ValueError, match="dataset_content_hash"):
        module.prepare_matrix(protocol, "registered-dataset", "b" * 64, tmp_path / "matrix",
                              budget=16, horizons=(10,), seeds=(17,))
    assert not (tmp_path / "matrix").exists()


def test_prepare_rejects_output_inside_registered_dataset(tmp_path):
    module = comparison_module()
    protocol, content_hash = protocol_file(tmp_path)
    registered = Path(json.loads(protocol.read_text())["dataset_path"])
    with pytest.raises(ValueError, match="inside the registered dataset"):
        module.prepare_matrix(protocol, "registered-dataset", content_hash, registered / "matrix",
                              budget=16, horizons=(10,), seeds=(0,))
    assert not (registered / "matrix").exists()


def test_aggregate_reports_paired_validation_without_test_metrics(tmp_path):
    module, matrix = prepared(tmp_path)
    exports = exported_manifests(tmp_path, matrix)
    summary = module.aggregate_matrix(tmp_path / "matrix" / "matrix.json", exports, tmp_path / "summary.json")
    assert len(summary["cases"]) == 8
    assert len(summary["pairs"]) == 4
    assert "average_cash_ratio" not in summary["cases"][0]["validation"]
    assert summary["pairs"][0]["delta_v2_minus_v1"]["sharpe"] == 0.


@pytest.mark.parametrize("change, message", [
    (lambda item: item["metrics"].update(test={"sharpe": 2.}), "test"),
    (lambda item: item["metrics"].pop("test"), "test"),
    (lambda item: item["metrics"]["validation"].pop("sharpe"), "validation"),
    (lambda item: item["config"].update(n_steps=16), "config"),
    (lambda item: item.update(session_timesteps=24), "timesteps"),
    (lambda item: item.update(dataset_content_hash="b" * 64), "dataset"),
    (lambda item: item.update(feature_version="different"), "environment"),
    (lambda item: item["runtime"].update(torch="different"), "runtime"),
])
def test_aggregate_rejects_leakage_and_control_mismatches(tmp_path, change, message):
    module, matrix = prepared(tmp_path)
    exports = exported_manifests(tmp_path, matrix)
    first = exports / f"{matrix['cases'][0]['id']}.json"
    document = json.loads(first.read_text())
    change(document["manifest"])
    first.write_text(json.dumps(document))
    with pytest.raises(ValueError, match=message):
        module.aggregate_matrix(tmp_path / "matrix" / "matrix.json", exports, tmp_path / "summary.json")


def test_aggregate_rejects_duplicate_case_exports(tmp_path):
    module, matrix = prepared(tmp_path)
    exports = exported_manifests(tmp_path, matrix)
    case_id = matrix["cases"][0]["id"]
    original = json.loads((exports / f"{case_id}.json").read_text())
    (exports / "duplicate.json").write_text(json.dumps({"case_id": case_id, **original}))
    with pytest.raises(ValueError, match="duplicate"):
        module.aggregate_matrix(tmp_path / "matrix" / "matrix.json", exports, tmp_path / "summary.json")
