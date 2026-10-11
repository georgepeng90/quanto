import hashlib
import importlib.metadata
import json
from types import SimpleNamespace

import pytest

from quanto.workbench.models import BatchItem, FrozenRun
from quanto.workbench.configuration import FrameworkBlocked, FrameworkConflict
from quanto.data.contracts import InstrumentId
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from quanto.workbench.runs import RunService
from quanto.workbench.run_contexts import fixed_context_from_manifest
from quanto.workbench.snapshots import SnapshotBuilder
from quanto.workbench.revisions import digest, json_bytes
from quanto.framework import canonical_hash
import quanto.workbench.snapshots as snapshots
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.backtest_cases import dataset_case
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.fixtures.strategy_declarations import etf_declaration


def _ready(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench")
    source = dataset_case(tmp_path / "source")
    dataset = store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    declaration = etf_declaration(name="test", auxiliary_sources=["helper.py"],
        parameter_schema={"type": "object", "additionalProperties": False,
                          "properties": {"window": {"type": "integer", "default": 2}}})
    files = {"strategy.py": b"from helper import value\ndef create_strategy(params): return value\n", "helper.py": b"value = 1\n", "strategy.json": json.dumps(declaration).encode()}
    strategy = store.register_strategy(files, declaration)
    spec = {"start": "2024-01-01", "end": "2024-01-03", "initial_cash": 1000, "seed": 7, "execution": {"commission_rate": 0, "minimum_commission": 0, "slippage_rate": 0, "max_stale_sessions": 0, "model": "next_open"}, "risk": {"max_total_weight": 1, "max_symbol_weight": 1, "group_caps": {}, "stress_scenarios": [], "pause_action": "block_new", "recovery_mode": "never"}, "metrics": {"annual_sessions": 252, "risk_free_rate": 0, "cash_rate": 0, "definition_version": "v1"}}
    member = InstrumentId(exchange="SZSE", instrument_type="etf", code="000001")
    assert BindingStore(store).get_asset_binding(dataset.id).resolve((member,))
    basket = BasketStore(store).create("test", (member,), "fixture selection", "snapshot-fixture")
    experiment = store.save_experiment(None, None, {"framework_contract_version": "multi-asset-v1",
        "basket_revision_ref": basket.ref.model_dump(mode="json"), "strategy_revision_id": strategy.id,
        "dataset_version_id": dataset.id, "trading_definition_ref": {"kind": "trading", "id": "etf-daily", "version": "1"},
        "strategy_params": {}, "backtest_spec": spec, "sample_purpose": "exploration"})
    return store, experiment, dataset, strategy


def test_submission_is_idempotent(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    assert draft.payload["strategy_params"] == {}
    service = RunService(store)
    first = service.submit(draft.id, draft.revision, "same", "web")
    assert json.loads(first.manifest_path.read_text())["effective_strategy_params"] == {"window": 2}
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
    registered_hash = hashlib.sha256((dataset.path / "prices.parquet").read_bytes()).hexdigest()
    (tmp_path / "source" / "prices.csv").write_text("changed")
    (strategy.path / "helper.py").write_text("changed")
    assert json.loads(run.manifest_path.read_text()) == before
    assert hashlib.sha256(run.manifest_path.read_bytes()).hexdigest() == original_hash == run.manifest_hash
    assert (run.manifest_path.parent / "strategy" / "helper.py").read_text() == "value = 1\n"
    assert hashlib.sha256((dataset.path / "prices.parquet").read_bytes()).hexdigest() == registered_hash
    assert "src/quanto/backtest/engine.py" in before["files"]
    assert SnapshotBuilder(store).verify(FrozenRun(run.run_id, run.manifest_path, run.manifest_hash))["run_id"] == run.run_id


def test_current_rerun_requires_registered_definition(tmp_path, monkeypatch):
    from quanto.workbench.definitions import DefinitionRegistry

    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "initial", "web")

    def removed_definition(self, ref):
        raise KeyError(ref)

    monkeypatch.setattr(DefinitionRegistry, "get", removed_definition)
    assert SnapshotBuilder(store).verify(FrozenRun(run.run_id, run.manifest_path, run.manifest_hash))["run_id"] == run.run_id
    with pytest.raises(FrameworkBlocked, match="DEFINITION_UNAVAILABLE"):
        RunService(store).rerun(run.run_id, "definition-upgraded", "web")
    assert len(store.list_jobs()) == 1


def test_migrated_schema_2_run_reruns_under_current_framework(tmp_path):
    store, draft, dataset, _ = _ready(tmp_path)
    original = RunService(store).submit(draft.id, draft.revision, "historical", "web")
    manifest = json.loads(original.manifest_path.read_bytes())
    manifest["schema_version"] = 2
    del manifest["framework_contract_version"]
    del manifest["resolved_configuration"]
    raw = json_bytes(manifest)
    original.manifest_path.write_bytes(raw)
    input_hash = digest(raw)
    binding = BindingStore(store).get_asset_binding(dataset.id)
    context = fixed_context_from_manifest(manifest, input_hash=input_hash,
        asset_binding=binding, basket_revision_ref=draft.payload["basket_revision_ref"])
    with store.transaction() as db:
        db.execute("UPDATE runs SET manifest_hash=? WHERE run_id=?", (input_hash, original.run_id))
        db.execute("UPDATE run_contexts SET context_json=?,context_hash=? WHERE job_id=?",
                   (json_bytes(context).decode(), canonical_hash(context), original.job_id))

    repeated = RunService(store).rerun(original.run_id, "current-rerun", "web")
    assert repeated.parent_run_id == original.run_id
    assert repeated.manifest_path != original.manifest_path
    assert json.loads(repeated.manifest_path.read_bytes())["schema_version"] == 3
    assert RunService(store).rerun(original.run_id, "current-rerun", "web").run_id == repeated.run_id


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
    assert not list((store.root / "snapshots").iterdir())


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
    assert not list((store.root / "snapshots").iterdir())


def test_submission_rechecks_registered_strategy_after_freeze(tmp_path, monkeypatch):
    store, draft, dataset, strategy = _ready(tmp_path)
    original = SnapshotBuilder.freeze

    def change_registered_source(self, *args, **kwargs):
        frozen = original(self, *args, **kwargs)
        (strategy.path / "helper.py").write_text("value = 99\n")
        return frozen

    monkeypatch.setattr(SnapshotBuilder, "freeze", change_registered_source)
    with pytest.raises(FrameworkConflict) as caught:
        RunService(store).submit(draft.id, draft.revision, "recheck", "web")
    assert caught.value.report.status == "blocked"
    assert store.list_jobs() == []
    assert not list((store.root / "snapshots").iterdir())
    assert (store.root / "dataset_objects" / dataset.content_hash).is_dir()


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
