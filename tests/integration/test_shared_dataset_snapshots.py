import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from quanto.workbench.models import BatchItem, FrozenRun
from quanto.workbench.configuration import FrameworkConflict
from quanto.workbench.dataset_objects import ensure_dataset_object, verify_dataset_object
from quanto.workbench.runs import RunService
from quanto.workbench.snapshots import SnapshotBuilder
import quanto.workbench.snapshots as snapshots
from tests.integration.test_snapshot_submission import _ready


def test_runs_share_one_verified_dataset_object(tmp_path):
    store, draft, dataset, _ = _ready(tmp_path)
    second = store.save_experiment(None, None, draft.payload)
    runs = RunService(store).submit_batch([BatchItem(draft.id, 1), BatchItem(second.id, 1)], "shared", "web")

    objects = list((store.root / "dataset_objects").iterdir())
    assert len(objects) == 1
    assert objects[0].name == dataset.content_hash
    for run in runs:
        manifest = SnapshotBuilder(store).verify(FrozenRun(run.run_id, run.manifest_path, run.manifest_hash))
        assert manifest["schema_version"] == 3
        assert manifest["dataset_ref"]["id"] == dataset.id
        assert manifest["dataset_ref"]["content_hash"] == dataset.content_hash
        assert not (run.manifest_path.parent / "dataset").exists()


def test_shared_object_tamper_blocks_replay(tmp_path):
    store, draft, dataset, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "original", "web")
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    name = next(name for name in manifest["dataset_ref"]["files"] if name.startswith("prices."))
    (store.root / "dataset_objects" / dataset.content_hash / name).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="hash mismatch"):
        RunService(store).rerun(run.run_id, "repeat", "web")


def test_existing_object_with_extra_file_is_rejected(tmp_path):
    store, draft, dataset, _ = _ready(tmp_path)
    RunService(store).submit(draft.id, draft.revision, "first", "web")
    (store.root / "dataset_objects" / dataset.content_hash / "extra.txt").write_text("unexpected")

    with pytest.raises(ValueError, match="file set"):
        RunService(store).submit(draft.id, draft.revision, "second", "web")


def test_batch_loads_shared_dataset_once(tmp_path, monkeypatch):
    store, draft, _, _ = _ready(tmp_path)
    second = store.save_experiment(None, None, draft.payload)
    original = snapshots.load_dataset
    calls = []

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(snapshots, "load_dataset", counted)
    RunService(store).submit_batch([BatchItem(draft.id, 1), BatchItem(second.id, 1)], "once", "web")
    assert len(calls) == 1


@pytest.mark.parametrize("bad_hash", ["../escape", "a" * 63, "A" * 64])
def test_dataset_reference_rejects_invalid_digest(tmp_path, bad_hash):
    store, _, dataset, _ = _ready(tmp_path)
    _, files = ensure_dataset_object(store.root, dataset)
    with pytest.raises(ValueError, match="reference invalid"):
        verify_dataset_object(store.root, {"version_id": dataset.id, "content_hash": bad_hash, "files": files})


def test_concurrent_publication_reuses_complete_object(tmp_path):
    store, _, dataset, _ = _ready(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _: ensure_dataset_object(store.root, dataset)[0], range(2)))
    assert paths[0] == paths[1]
    assert [item.name for item in (store.root / "dataset_objects").iterdir()] == [dataset.content_hash]


def test_final_recheck_blocks_source_changes_but_preserves_shared_object(tmp_path, monkeypatch):
    store, draft, dataset, _ = _ready(tmp_path)
    original = snapshots.ensure_dataset_object

    def source_changes(workspace, version):
        value = original(workspace, version)
        (dataset.path / "prices.parquet").write_bytes(b"source changed")
        return value

    monkeypatch.setattr(snapshots, "ensure_dataset_object", source_changes)
    with pytest.raises(FrameworkConflict) as caught:
        RunService(store).submit(draft.id, draft.revision, "source-change", "web")
    assert "BINDING_MISMATCH" in caught.value.report.error_codes
    assert store.list_jobs() == []
    assert not list((store.root / "snapshots").iterdir())
    assert (store.root / "dataset_objects" / dataset.content_hash).is_dir()


def test_tamper_after_freeze_prevents_batch_queue(tmp_path, monkeypatch):
    store, draft, dataset, _ = _ready(tmp_path)
    original = SnapshotBuilder.freeze

    def tamper(self, *args, **kwargs):
        frozen = original(self, *args, **kwargs)
        (store.root / "dataset_objects" / dataset.content_hash / "manifest.json").write_text("changed")
        return frozen

    monkeypatch.setattr(SnapshotBuilder, "freeze", tamper)
    with pytest.raises(FrameworkConflict) as caught:
        RunService(store).submit(draft.id, draft.revision, "tamper-before-queue", "web")
    assert "hash mismatch" in str(caught.value.__cause__)
    assert store.list_jobs() == []
