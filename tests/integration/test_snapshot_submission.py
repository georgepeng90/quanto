import hashlib
import importlib.metadata
import json
from types import SimpleNamespace

import pytest

from quanto.workbench.models import BatchItem, FrozenRun
from quanto.workbench.runs import RunService
from quanto.workbench.snapshots import SnapshotBuilder
import quanto.workbench.snapshots as snapshots
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.backtest_cases import dataset_case


def _ready(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench")
    dataset = store.register_dataset(dataset_case(tmp_path / "source"))
    declaration = {"name": "test", "entrypoint": "strategy.py:create_strategy", "parameter_schema": {"type": "object", "properties": {"window": {"type": "integer", "default": 2}}}, "required_data_columns": ["date", "symbol", "close"], "auxiliary_sources": ["helper.py"], "research_data": []}
    files = {"strategy.py": b"from helper import value\ndef create_strategy(params): return value\n", "helper.py": b"value = 1\n", "strategy.json": json.dumps(declaration).encode()}
    strategy = store.register_strategy(files, declaration)
    spec = {"start": "2024-01-01", "end": "2024-01-03", "symbols": ["000001"], "initial_cash": 1000, "seed": 7, "execution": {"commission_rate": 0, "minimum_commission": 0, "slippage_rate": 0, "max_stale_sessions": 0, "model": "next_open"}, "risk": {"max_total_weight": 1, "max_symbol_weight": 1, "group_caps": {}, "stress_scenarios": [], "pause_action": "block_new", "recovery_mode": "never"}, "metrics": {"annual_sessions": 252, "risk_free_rate": 0, "cash_rate": 0, "definition_version": "v1"}}
    experiment = store.save_experiment(None, None, {"strategy_revision_id": strategy.id, "dataset_version_id": dataset.id, "strategy_params": {}, "backtest_spec": spec, "sample_purpose": "exploration"})
    return store, experiment, dataset, strategy


def test_submission_is_idempotent(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    assert draft.payload["strategy_params"] == {}
    assert draft.payload["effective_strategy_params"] == {"window": 2}
    service = RunService(store)
    first = service.submit(draft.id, draft.revision, "same", "web")
    assert first.run_id == service.submit(draft.id, draft.revision, "same", "web").run_id
    assert len(store.list_jobs()) == 1
    with pytest.raises(ValueError, match="idempotency"):
        service.submit(draft.id, draft.revision, "same", "agent")


def test_failed_freeze_never_queues(tmp_path, monkeypatch):
    store, draft, _, _ = _ready(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(SnapshotBuilder, "freeze", fail)
    with pytest.raises(OSError, match="disk full"):
        RunService(store).submit(draft.id, draft.revision, "freeze-fails", "web")
    assert store.list_jobs() == []


def test_snapshot_ignores_later_workspace_edits(tmp_path):
    store, draft, dataset, strategy = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "frozen", "web")
    before = json.loads(run.manifest_path.read_text())
    original_hash = hashlib.sha256(run.manifest_path.read_bytes()).hexdigest()
    (tmp_path / "source" / "prices.csv").write_text("changed")
    (strategy.path / "helper.py").write_text("changed")
    assert json.loads(run.manifest_path.read_text()) == before
    assert hashlib.sha256(run.manifest_path.read_bytes()).hexdigest() == original_hash == run.manifest_hash
    assert (run.manifest_path.parent / "strategy" / "helper.py").read_text() == "value = 1\n"
    assert (dataset.path / "prices.csv").read_text() != "changed"
    assert "src/quanto/backtest/engine.py" in before["files"]
    assert SnapshotBuilder(store).verify(FrozenRun(run.run_id, run.manifest_path, run.manifest_hash))["run_id"] == run.run_id


def test_batch_preflight_and_ordered_retry(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    second = store.save_experiment(None, None, draft.payload)
    service = RunService(store)
    with pytest.raises(ValueError, match="revision"):
        service.submit_batch([BatchItem(draft.id, 1), BatchItem(second.id, 2)], "batch", "web")
    assert store.list_jobs() == []
    items = [BatchItem(draft.id, 1), BatchItem(second.id, 1)]
    runs = service.submit_batch(items, "batch", "web")
    assert [r.run_id for r in runs] == [r.run_id for r in service.submit_batch(items, "batch", "web")]
    assert [j.enqueue_seq for j in store.list_jobs()] == [1, 2]
    with pytest.raises(ValueError, match="idempotency"):
        service.submit_batch(list(reversed(items)), "batch", "web")


def test_second_batch_freeze_failure_exposes_no_jobs(tmp_path, monkeypatch):
    store, draft, _, _ = _ready(tmp_path)
    second = store.save_experiment(None, None, draft.payload)
    original = SnapshotBuilder.freeze
    calls = 0
    def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SnapshotBuilder, "freeze", fail_second)
    with pytest.raises(OSError, match="disk full"):
        RunService(store).submit_batch([BatchItem(draft.id, 1), BatchItem(second.id, 1)], "second-fails", "web")
    assert store.list_jobs() == []


def test_queue_insert_failure_rolls_back_all_runs(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    with store.transaction() as db:
        db.execute("CREATE TRIGGER reject_queue BEFORE INSERT ON jobs BEGIN SELECT RAISE(FAIL, 'queue failed'); END")
    with pytest.raises(Exception, match="queue failed"):
        RunService(store).submit(draft.id, draft.revision, "queue-fails", "web")
    assert store.list_jobs() == []
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 0


def test_undeclared_research_data_cannot_be_frozen(tmp_path):
    store, draft, _, strategy = _ready(tmp_path)
    declaration = dict(strategy.manifest, research_data=["missing_research"])
    files = {"strategy.py": (strategy.path / "strategy.py").read_bytes(), "helper.py": (strategy.path / "helper.py").read_bytes(), "strategy.json": json.dumps(declaration).encode()}
    new_strategy = store.register_strategy(files, declaration)
    revised = store.save_experiment(draft.id, draft.revision, dict(draft.payload, strategy_revision_id=new_strategy.id))
    with pytest.raises(ValueError, match="unregistered research data"):
        RunService(store).submit(revised.id, revised.revision, "research", "web")
    assert store.list_jobs() == []


def test_snapshot_verification_rejects_added_execution_file(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "verify", "web")
    extra = run.manifest_path.parent / "src" / "quanto" / "backtest" / "unexpected.py"
    extra.write_text("value = 1\n")
    with pytest.raises(ValueError, match="file set mismatch"):
        SnapshotBuilder(store).verify(FrozenRun(run.run_id, run.manifest_path, run.manifest_hash))


def test_exact_verification_detects_installed_distribution_drift(tmp_path, monkeypatch):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "dist", "web")
    frozen = FrozenRun(run.run_id, run.manifest_path, run.manifest_hash)
    environment = json.loads(run.manifest_path.read_text())["environment"]
    assert environment["installed_distributions"]
    original = importlib.metadata.distributions
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [*original(), SimpleNamespace(metadata={"Name": "unexpected-package"}, version="1.0")])
    with pytest.raises(ValueError, match="environment changed"):
        SnapshotBuilder(store).verify(frozen)


@pytest.mark.parametrize("changed_name", ["src/quanto/backtest/new_module.py", "uv.lock"])
def test_freeze_rejects_execution_file_set_or_lock_drift(tmp_path, monkeypatch, changed_name):
    store, draft, _, _ = _ready(tmp_path)
    original = snapshots._execution_files
    calls = 0
    def changing_files(project_root):
        nonlocal calls
        calls += 1
        files = original(project_root)
        if calls == 2:
            files[changed_name] = b"changed during freeze"
        return files
    monkeypatch.setattr(snapshots, "_execution_files", changing_files)
    with pytest.raises(ValueError, match="engine or environment files changed"):
        RunService(store).submit(draft.id, draft.revision, "drift", "web")
    assert store.list_jobs() == []


def test_recorded_lock_hash_matches_frozen_bytes(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "lock", "web")
    manifest = json.loads(run.manifest_path.read_text())
    lock_hash = hashlib.sha256((run.manifest_path.parent / "uv.lock").read_bytes()).hexdigest()
    assert manifest["environment"]["lock_hashes"]["uv.lock"] == manifest["files"]["uv.lock"] == lock_hash


def test_freeze_rejects_post_publication_source_drift(tmp_path, monkeypatch):
    store, draft, _, _ = _ready(tmp_path)
    original = snapshots._execution_files
    calls = 0
    def changed_after_publication(project_root):
        nonlocal calls
        calls += 1
        files = original(project_root)
        if calls == 3:
            files["src/quanto/backtest/new_module.py"] = b"late edit"
        return files
    monkeypatch.setattr(snapshots, "_execution_files", changed_after_publication)
    with pytest.raises(ValueError, match="engine or environment files changed"):
        RunService(store).submit(draft.id, draft.revision, "late-drift", "web")
    assert store.list_jobs() == []
