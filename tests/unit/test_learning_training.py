from datetime import timedelta

import numpy as np
import pytest

from tests.fixtures.learning_cases import learning_case, training_case


def test_scaler_is_train_only_and_labels_are_purged(tmp_path):
    from quanto.learning.dataset import prepare_dataset
    from quanto.learning.windows import WindowStore
    from quanto.learning.contracts import TrainingSpec
    spec, start = learning_case(tmp_path / "market", spike_after=None)
    prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    config = TrainingSpec.model_validate(training_case(start))
    with WindowStore(tmp_path / "dataset", tmp_path / "cache", config) as windows:
        rows = windows.rows
        mask = (rows["end"] >= int(start.timestamp() * 1e6)) & (rows["end"] < int((start + timedelta(minutes=35)).timestamp() * 1e6))
        np.testing.assert_allclose(windows.scaler["mean"], windows.features[mask].mean(axis=0), rtol=1e-5, atol=1e-6)
        for split, end in (("train", 35), ("validation", 60), ("test", 90)):
            indices = windows.indices(split)
            assert len(indices) > 0
            assert all(windows.samples[index]["label_end"] <= int((start + timedelta(minutes=end)).timestamp() * 1e6) for index in indices)
        sample = windows.samples[windows.indices("train")[0]]
        assert sample["label"] == pytest.approx(np.log(rows[sample["target"]]["close"] / rows[sample["next"]]["open"]), rel=1e-5)


@pytest.mark.parametrize("model_type", ["linear", "gru"])
def test_real_training_reload_resume_and_tamper_rejection(tmp_path, model_type):
    from quanto.learning.dataset import prepare_dataset
    from quanto.learning.training import train_model
    from quanto.learning.inference import iter_inference_rows
    spec, start = learning_case(tmp_path / "market")
    prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    config = training_case(start, model_type=model_type, epochs=1)
    first = train_model(config, tmp_path / "dataset", tmp_path / "model", lambda event: None, lambda: False)
    assert first["metrics"]["samples"]["test"] > 0
    predictions = list(iter_inference_rows(tmp_path / "model", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    repeated = list(iter_inference_rows(tmp_path / "model", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    assert predictions == repeated
    assert predictions[0]["score"] is None
    assert np.isfinite(predictions[4]["score"])
    config["epochs"] = 2
    resumed = train_model(config, tmp_path / "dataset", tmp_path / "resumed", lambda event: None, lambda: False, resume_dir=tmp_path / "model")
    assert resumed["epochs_completed"] == 2
    (tmp_path / "model" / "weights.pt").write_bytes(b"tamper")
    with pytest.raises(ValueError, match="hash|integrity"):
        list(iter_inference_rows(tmp_path / "model", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    with pytest.raises(ValueError, match="hash|integrity"):
        train_model(config, tmp_path / "dataset", tmp_path / "tampered-resume", lambda event: None, lambda: False, tmp_path / "model")


def test_cancelled_training_can_resume_exactly_and_rejects_changed_identity(tmp_path):
    from quanto.learning.contracts import LearningCancelled
    from quanto.learning.dataset import prepare_dataset
    from quanto.learning.training import train_model
    from quanto.learning.inference import iter_inference_rows
    spec, start = learning_case(tmp_path / "market")
    prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    config = training_case(start, model_type="gru", epochs=2)
    is_cancelled = False

    def emit(event):
        nonlocal is_cancelled
        if event.get("epoch") == 1:
            is_cancelled = True

    with pytest.raises(LearningCancelled):
        train_model(config, tmp_path / "dataset", tmp_path / "interrupted", emit, lambda: is_cancelled)
    assert (tmp_path / "interrupted" / "checkpoints" / "checkpoint.json").exists()
    assert not (tmp_path / "interrupted" / "manifest.json").exists()
    assert not (tmp_path / "interrupted" / "window-cache").exists()
    with pytest.raises(ValueError, match="identity"):
        train_model({**config, "horizon": 3}, tmp_path / "dataset", tmp_path / "invalid", lambda event: None, lambda: False, tmp_path / "interrupted")
    train_model(config, tmp_path / "dataset", tmp_path / "resumed", lambda event: None, lambda: False, tmp_path / "interrupted")
    train_model(config, tmp_path / "dataset", tmp_path / "straight", lambda event: None, lambda: False)
    resumed = list(iter_inference_rows(tmp_path / "resumed", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    straight = list(iter_inference_rows(tmp_path / "straight", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    assert resumed == straight


def test_requested_cuda_is_never_silently_downgraded(monkeypatch):
    import torch
    from quanto.learning.models import resolve_device
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert resolve_device("auto") == "cpu"
    with pytest.raises(ValueError, match="CUDA.*unavailable"):
        resolve_device("cuda")
