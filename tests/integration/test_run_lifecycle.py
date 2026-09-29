from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest

from quanto.workbench.runs import RunService
from quanto.workbench.scheduler import Scheduler
from quanto.workbench.worker import EventWriter, atomic_json, read_partial, wait_for_ack
from quanto.workbench.snapshots import SnapshotBuilder
from tests.integration.test_snapshot_submission import _ready


def queued(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, 1, "lifecycle", "web")
    return store, run


def test_job_claim_is_exclusive(tmp_path):
    store, run = queued(tmp_path)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: store.claim_job(), range(2)))
    assert [job.id for job in claims if job] == [run.job_id]


@pytest.mark.parametrize("first", ["cancel", "complete"])
def test_cancel_and_completion_cannot_overwrite_terminal_state(tmp_path, first):
    store, run = queued(tmp_path)
    store.claim_job()
    if first == "complete":
        assert store.finish_job(run.job_id, "succeeded", expected="running")
    RunService(store).cancel(run.run_id)
    if first == "cancel":
        assert store.get_job(run.job_id).state == "cancel_requested"
        assert not store.finish_job(run.job_id, "succeeded", expected="running")
        assert store.finish_job(run.job_id, "cancelled", expected="cancel_requested")
    assert not store.finish_job(run.job_id, "failed", expected="running")
    assert store.get_job(run.job_id).state == ("cancelled" if first == "cancel" else "succeeded")


def test_cancelled_queued_job_never_starts(tmp_path):
    store, run = queued(tmp_path)
    RunService(store).cancel(run.run_id)
    assert store.claim_job() is None
    assert store.get_job(run.job_id).state == "cancelled"


def test_restart_marks_abandoned_job_interrupted(tmp_path):
    store, run = queued(tmp_path)
    store.claim_job()
    draft = store.get_experiment(run.experiment_id)
    other = RunService(store).submit(draft.id, 1, "other", "web")
    scheduler = Scheduler(store)
    scheduler.recover()
    assert store.get_job(run.job_id).state == "interrupted"
    assert store.get_job(other.job_id).state == "queued"


def test_service_lock_prevents_second_scheduler(tmp_path):
    store, _ = queued(tmp_path)
    first, second = Scheduler(store), Scheduler(store)
    first.start(background=False)
    try:
        with pytest.raises(RuntimeError, match="SCHEDULER_LOCKED"):
            second.start(background=False)
    finally:
        first.stop()


def test_spawn_failure_does_not_run_next_queued_job(tmp_path):
    store, run = queued(tmp_path)
    other = RunService(store).submit(run.experiment_id, 1, "other", "web")
    def broken(*args, **kwargs):
        raise OSError("spawn unavailable")
    scheduler = Scheduler(store, launcher=broken)
    scheduler.start(background=False)
    try:
        scheduler.tick()
        assert store.get_job(run.job_id).state == "interrupted"
        assert store.get_job(other.job_id).state == "queued"
    finally:
        scheduler.stop()


def test_unacknowledged_child_exits_without_strategy_entry(tmp_path):
    now = [0.0]
    descriptor = {"job_id": "job", "parent_pid": 1, "parent_start": "start", "handshake_timeout": 0.04}
    assert not wait_for_ack(tmp_path, descriptor, clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0] + n), alive=lambda *args: True)
    atomic_json(tmp_path / "ack.json", {"job_id": "job"})
    assert not wait_for_ack(tmp_path, descriptor, alive=lambda *args: False)


def test_restart_does_not_kill_reused_pid(tmp_path, monkeypatch):
    store, run = queued(tmp_path)
    store.claim_job()
    store.register_process(run.job_id, os.getpid(), "different-creation-time")
    Scheduler(store).recover()
    job = store.get_job(run.job_id)
    assert job.state == "interrupted"
    assert job.error.startswith("PROCESS_IDENTITY_CHANGED")


def test_completed_day_upserts_order_and_durably_advances_progress(tmp_path):
    writer = EventWriter(tmp_path)
    for day, state in [("2024-01-01", "pending"), ("2024-01-02", "filled")]:
        writer.emit({"payload": {"completed_through": day, "tables": {
            "equity": [{"date": day, "equity": 1000}], "orders": [{"id": "one", "status": state}]}}})
    result = read_partial(tmp_path)
    assert result["completed_through"] == "2024-01-02"
    assert result["tables"]["orders"] == [{"id": "one", "status": "filled"}]
    assert len(result["tables"]["equity"]) == 2
    assert len((tmp_path / "events.ndjson").read_text().splitlines()) == 2


class FakeProcess:
    pid = 2147483000
    returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout):
        assert self.returncode is not None
        return self.returncode


def test_cancel_timeout_requires_exit_before_cancelled(tmp_path):
    store, run = queued(tmp_path)
    now = [0.0]
    child = FakeProcess()
    scheduler = Scheduler(store, launcher=lambda *a, **k: child, clock=lambda: now[0], cancel_timeout=0.05)
    scheduler.start(background=False)
    try:
        scheduler.tick()
        RunService(store).cancel(run.run_id)
        scheduler.tick()
        assert store.get_job(run.job_id).state == "cancel_requested"
        assert child.poll() is None
        now[0] = 0.06
        scheduler.tick()
        scheduler.tick()
        assert child.poll() == -9
        assert store.get_job(run.job_id).state == "cancelled"
    finally:
        scheduler.stop()


def test_stuck_handshake_and_shutdown_reap_owned_children(tmp_path):
    store, run = queued(tmp_path)
    now = [0.0]
    child = FakeProcess()
    scheduler = Scheduler(store, launcher=lambda *a, **k: child, clock=lambda: now[0], handshake_timeout=0.05)
    scheduler.start(background=False)
    scheduler.tick()
    now[0] = 0.06
    scheduler.tick()
    scheduler.tick()
    assert store.get_job(run.job_id).state == "failed"
    scheduler.stop()
    assert child.poll() == -9


def test_live_heartbeat_keeps_silent_computation_running(tmp_path):
    store, run = queued(tmp_path)
    now = [0.0]
    child = FakeProcess()
    scheduler = Scheduler(store, launcher=lambda *a, **k: child, clock=lambda: now[0], heartbeat_timeout=0.05)
    scheduler.start(background=False)
    try:
        scheduler.tick()
        scheduler._active[run.job_id].acknowledged = True
        now[0] = 100.0
        control = store.artifact_root / run.job_id / "control"
        atomic_json(control / "heartbeat.json", {"job_id": run.job_id, "at": "now", "monotonic": 100.0})
        scheduler.tick()
        assert child.poll() is None
        now[0] = 100.06
        scheduler.tick()
        assert child.poll() == -9
    finally:
        scheduler.stop()


def test_paused_snapshot_freeze_does_not_block_cancellation(tmp_path, monkeypatch):
    store, run = queued(tmp_path)
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original = SnapshotBuilder.freeze
    def paused(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SnapshotBuilder, "freeze", paused)
    with ThreadPoolExecutor(2) as pool:
        submission = pool.submit(RunService(store).submit, run.experiment_id, 1, "concurrent", "web")
        assert entered.wait(2)
        def cancel():
            RunService(store).cancel(run.run_id)
            cancelled.set()
        cancellation = pool.submit(cancel)
        try:
            assert cancelled.wait(1), "snapshot held the lifecycle write lock"
        finally:
            release.set()
            submission.result(timeout=10)
            cancellation.result(timeout=10)


def test_revision_change_during_freeze_prevents_queue_publication(tmp_path, monkeypatch):
    store, run = queued(tmp_path)
    original = SnapshotBuilder.freeze
    def changed(self, experiment, *args, **kwargs):
        frozen = original(self, experiment, *args, **kwargs)
        store.save_experiment(experiment.id, experiment.revision, experiment.payload)
        return frozen
    monkeypatch.setattr(SnapshotBuilder, "freeze", changed)
    with pytest.raises(ValueError, match="revision"):
        RunService(store).submit(run.experiment_id, 1, "changed", "web")
    assert len(store.list_jobs()) == 1


def test_concurrent_submission_rechecks_idempotency_after_freeze(tmp_path, monkeypatch):
    store, run = queued(tmp_path)
    barrier = threading.Barrier(2)
    original = SnapshotBuilder.freeze
    def paused(self, *args, **kwargs):
        frozen = original(self, *args, **kwargs)
        barrier.wait(timeout=5)
        return frozen
    monkeypatch.setattr(SnapshotBuilder, "freeze", paused)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(RunService(store).submit, run.experiment_id, 1, "same-racing", "web") for _ in range(2)]
        runs = [future.result(timeout=10) for future in futures]
    assert runs[0].run_id == runs[1].run_id
    assert len(store.list_jobs()) == 2


def test_shutdown_reaps_every_child_even_if_database_is_unavailable(tmp_path, monkeypatch):
    store, run = queued(tmp_path)
    RunService(store).submit(run.experiment_id, 1, "second", "web")
    children = [FakeProcess(), FakeProcess()]
    launches = iter(children)
    scheduler = Scheduler(store, max_workers=2, launcher=lambda *a, **k: next(launches))
    scheduler.start(background=False)
    scheduler.tick()
    def unavailable(*args, **kwargs):
        raise OSError("database unavailable")
    monkeypatch.setattr(store, "checkpoint", unavailable)
    with pytest.raises(OSError, match="database unavailable"):
        scheduler.stop()
    assert all(child.poll() == -9 for child in children)


def test_scheduler_leaves_full_input_scan_to_heartbeat_worker(tmp_path, monkeypatch):
    store, run = queued(tmp_path)
    def expensive_scan(*args, **kwargs):
        raise AssertionError("full dataset scan belongs in child")
    monkeypatch.setattr(SnapshotBuilder, "verify", expensive_scan)
    scheduler = Scheduler(store, launcher=lambda *a, **k: FakeProcess())
    scheduler.start(background=False)
    try:
        scheduler.tick()
        assert store.get_job(run.job_id).state == "running"
    finally:
        scheduler.stop()
