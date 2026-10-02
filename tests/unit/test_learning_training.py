from datetime import datetime, timedelta, timezone

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


def test_resuming_early_stopped_checkpoint_does_not_train_another_epoch(tmp_path):
    from quanto.learning.dataset import prepare_dataset
    from quanto.learning.training import train_model, read_checkpoint
    from quanto.learning.inference import iter_inference_rows
    spec, start = learning_case(tmp_path / "market")
    prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    config = training_case(start, epochs=20, learning_rate=.1, patience=1)
    first = train_model(config, tmp_path / "dataset", tmp_path / "first", lambda event: None, lambda: False)
    assert first["epochs_completed"] < config["epochs"]
    assert read_checkpoint(tmp_path / "first")["stale"] >= config["patience"]
    events = []
    resumed = train_model({**config, "epochs": 30}, tmp_path / "dataset", tmp_path / "resumed", events.append, lambda: False, tmp_path / "first")
    assert resumed["epochs_completed"] == first["epochs_completed"]
    assert resumed["best_epoch"] == first["best_epoch"]
    assert not any("epoch" in event for event in events)
    assert read_checkpoint(tmp_path / "resumed")["epoch"] == first["epochs_completed"]
    before = list(iter_inference_rows(tmp_path / "first", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    after = list(iter_inference_rows(tmp_path / "resumed", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    assert before == after


def test_training_and_inference_use_same_observed_steps_across_missing_minutes(tmp_path):
    import torch
    from quanto.learning.contracts import TrainingSpec
    from quanto.learning.dataset import prepare_dataset
    from quanto.learning.features import FEATURE_NAMES
    from quanto.learning.models import build_model
    from quanto.learning.training import train_model
    from quanto.learning.inference import iter_inference_rows
    from quanto.learning.windows import WindowStore
    spec, start = learning_case(tmp_path / "market", missing=(datetime(2024, 1, 2, 1, 34, tzinfo=timezone.utc),))
    dataset = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    config = training_case(start, epochs=1)
    manifest = train_model(config, tmp_path / "dataset", tmp_path / "model", lambda event: None, lambda: False)
    assert dataset["step_policy"] == manifest["step_policy"] == "observed_bar_steps"
    rows = list(iter_inference_rows(tmp_path / "model", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
    assert rows[4]["bar_start"] == (start + timedelta(minutes=5)).isoformat()
    assert rows[4]["score"] is not None
    with WindowStore(tmp_path / "dataset", tmp_path / "cache", TrainingSpec.model_validate(config)) as windows:
        first_sample = windows.samples[windows.indices("train")[0]]
        assert first_sample["anchor"] == 3
        assert first_sample["label_end"] == int((start + timedelta(minutes=7)).timestamp() * 1e6)
        model = build_model(TrainingSpec.model_validate(config), len(FEATURE_NAMES))
        model.load_state_dict(torch.load(tmp_path / "model" / "weights.pt", weights_only=True))
        model.eval()
        with torch.inference_mode():
            score = float(model(torch.from_numpy(windows.window(4)[None]))[0])
        assert rows[4]["score"] == pytest.approx(score, abs=1e-7)
    from quanto.objects import digest, json_bytes
    manifest["step_policy"] = "wall_clock_steps"
    manifest["id"] = digest(json_bytes({key: value for key, value in manifest.items() if key != "id"}))
    (tmp_path / "model" / "manifest.json").write_bytes(json_bytes(manifest))
    with pytest.raises(ValueError, match="step policy"):
        list(iter_inference_rows(tmp_path / "model", tmp_path / "dataset", start, start + timedelta(minutes=90), lambda: False))
