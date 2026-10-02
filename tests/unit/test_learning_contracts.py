import pytest

from tests.fixtures.learning_cases import learning_case, training_case


def test_contracts_reject_overlap_naive_time_and_unsupported_frequencies(tmp_path):
    from quanto.learning.contracts import PrepareSpec, TrainingSpec
    spec, start = learning_case(tmp_path / "market")
    with pytest.raises(ValueError):
        PrepareSpec.model_validate({**spec, "frequency_minutes": 2})
    with pytest.raises(ValueError, match="ordered"):
        TrainingSpec.model_validate(training_case(start, validation_start=start.isoformat()))
    with pytest.raises(ValueError, match="timezone"):
        TrainingSpec.model_validate(training_case(start, train_start=start.replace(tzinfo=None).isoformat()))


def test_preparation_cancellation_never_creates_a_complete_manifest(tmp_path):
    from quanto.learning.contracts import LearningCancelled
    from quanto.learning.dataset import prepare_dataset
    spec, _ = learning_case(tmp_path / "market")
    with pytest.raises(LearningCancelled):
        prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: True)
    assert not (tmp_path / "dataset" / "manifest.json").exists()


def test_mapped_windows_release_files_on_cancellation(tmp_path):
    from quanto.learning.contracts import LearningCancelled, TrainingSpec
    from quanto.learning.dataset import prepare_dataset
    from quanto.learning.windows import WindowStore
    import shutil
    spec, start = learning_case(tmp_path / "market")
    prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    with pytest.raises(LearningCancelled):
        WindowStore(tmp_path / "dataset", tmp_path / "cache", TrainingSpec.model_validate(training_case(start)), lambda: True)
    shutil.rmtree(tmp_path / "cache")


def test_dataset_manifest_rejects_changed_feature_definition(tmp_path):
    import json
    from quanto.learning.dataset import prepare_dataset, read_dataset_manifest
    from quanto.objects import digest, json_bytes
    spec, _ = learning_case(tmp_path / "market")
    manifest = prepare_dataset(spec, tmp_path / "market", tmp_path / "dataset", lambda event: None, lambda: False)
    manifest["features"].reverse()
    manifest["id"] = digest(json_bytes({key: value for key, value in manifest.items() if key != "id"}))
    (tmp_path / "dataset" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        read_dataset_manifest(tmp_path / "dataset")


def test_window_cache_cleanup_rejects_targets_outside_output_directory(tmp_path):
    from quanto.learning.training import _remove_window_cache
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "preserved.txt"
    marker.write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        _remove_window_cache(outside, output)
    assert marker.read_text(encoding="utf-8") == "preserve"
    with pytest.raises(ValueError, match="outside"):
        _remove_window_cache(output, output)
    cache = output / "window-cache"
    cache.mkdir()
    (cache / "temporary.txt").write_text("temporary", encoding="utf-8")
    _remove_window_cache(cache, output)
    assert not cache.exists()
