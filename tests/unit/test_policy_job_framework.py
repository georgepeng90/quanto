import json
from pathlib import Path

import pytest

from quanto.investing.jobs import execute_policy_job
from quanto.investing.models import InvestingModels
from quanto.workbench.service import WorkbenchService
from tests.integration.test_framework_capabilities import _training_case


def _submitted(tmp_path):
    store, request, _ = _training_case(tmp_path)
    models = InvestingModels(WorkbenchService(store.root))
    model = models.create_model(request.model_dump(mode="json"))
    job = models.train_model(model["id"], {"additional_timesteps": 8})
    descriptor = json.loads(Path(store.get_job(job["id"]).input_path).read_bytes())
    return descriptor["input"], descriptor["source_hashes"]


def test_policy_job_injects_frozen_model_identity(tmp_path, monkeypatch):
    payload, source_hashes = _submitted(tmp_path)
    called = []
    class RuntimeObserved(Exception):
        pass
    def observe(*args, **kwargs):
        called.append(kwargs)
        raise RuntimeObserved
    monkeypatch.setattr("quanto.investing.training.train_policy", observe)
    with pytest.raises(RuntimeObserved):
        execute_policy_job("train_policy", payload, tmp_path / "artifact", lambda event: None, lambda: False,
                           source_hashes=source_hashes)
    runtime = called[0]["learning_runtime"]
    assert called[0]["model_id"] == payload["request"]["model_id"]
    assert runtime.binding.binding_hash == payload["model_binding"]["training_configuration"]["asset_binding"]["binding_hash"]


@pytest.mark.parametrize("change", ["missing_binding", "changed_spec", "changed_dataset"])
def test_policy_job_rejects_unbound_or_changed_inputs_before_training(tmp_path, monkeypatch, change):
    payload, source_hashes = _submitted(tmp_path)
    if change == "missing_binding":
        del payload["model_binding"]
    elif change == "changed_spec":
        payload["request"]["spec"]["lookback"] += 1
    else:
        payload["dataset_ref"]["id"] = "another-version"
    def forbidden(*args, **kwargs):
        raise AssertionError("training must not execute")
    monkeypatch.setattr("quanto.investing.training.train_policy", forbidden)
    with pytest.raises(ValueError, match="MODEL_IDENTITY_MISMATCH"):
        execute_policy_job("train_policy", payload, tmp_path / "artifact", lambda event: None, lambda: False,
                           source_hashes=source_hashes)
