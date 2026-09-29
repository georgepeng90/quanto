import json
import time
import importlib.metadata
import pytest
import subprocess
import sys
import psutil
from types import SimpleNamespace

from quanto.workbench.runs import RunService
from quanto.workbench.scheduler import Scheduler
from quanto.workbench.worker import read_partial
from tests.integration.test_snapshot_submission import _ready


def test_environment_is_independent_of_duplicate_import_paths(tmp_path, monkeypatch):
    from quanto.workbench.snapshots import _environment, _metadata_files
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    expected = _environment(root, _metadata_files(root))
    installed = list(importlib.metadata.distributions())
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: installed + installed)
    assert _environment(root, _metadata_files(root)) == expected


STRATEGY = '''from quanto.strategies.base import Hold
from helper import value
import random
import numpy as np
initial = (random.random(), float(np.random.random()))
class Strategy:
    warmup_bars = 0
    def should_evaluate(self, ctx): return True
    def on_close(self, ctx):
        assert value == 1
        assert initial == (0.32383276483316237, 0.07630828937395717)
        return Hold("冻结策略")
def create_strategy(params): return Strategy()
'''


def ready_run(tmp_path, source=STRATEGY, seed=7, extra_files=None):
    store, draft, _, original = _ready(tmp_path)
    declaration = dict(original.manifest)
    declaration["auxiliary_sources"] = [*declaration["auxiliary_sources"], *(extra_files or {})]
    files = {"strategy.py": source.encode(), "helper.py": b"value = 1\n", "strategy.json": json.dumps(declaration).encode(), **(extra_files or {})}
    revision = store.register_strategy(files, declaration)
    payload = dict(draft.payload, strategy_revision_id=revision.id)
    payload["backtest_spec"]["seed"] = seed
    draft = store.save_experiment(draft.id, 1, payload)
    run = RunService(store).submit(draft.id, draft.revision, "execute", "web")
    return store, run, revision


def run_until_finished(store, job_id, timeout=20):
    scheduler = Scheduler(store, poll_interval=0.01)
    scheduler.start(background=False)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            scheduler.tick()
            job = store.get_job(job_id)
            if job.state in {"succeeded", "failed", "cancelled", "interrupted"}:
                return job
            time.sleep(0.01)
        raise AssertionError("worker did not exit")
    finally:
        scheduler.stop()


def test_worker_imports_frozen_modules(tmp_path, monkeypatch):
    store, run, strategy = ready_run(tmp_path)
    (strategy.path / "helper.py").write_text("value = 999")
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "helper.py").write_text("raise AssertionError('mutable import')")
    monkeypatch.setenv("PYTHONPATH", str(poison))
    job = run_until_finished(store, run.job_id)
    assert job.state == "succeeded", job.error
    root = store.artifact_root / run.job_id
    runtime = json.loads((root / "control" / "runtime.json").read_text())
    assert str(run.manifest_path.parent) in runtime["engine_file"]
    assert str(run.manifest_path.parent) in runtime["strategy_file"]
    assert not list(run.manifest_path.parent.rglob("__pycache__"))
    metadata = json.loads((root / "final" / "metadata.json").read_text())
    assert metadata["strategy_revision_id"] == strategy.id
    html = (root / "final" / "report.html").read_text(encoding="utf-8")
    assert strategy.id in html and metadata["dataset_version_id"] in html and "raw_cash" in html
    logs = [json.loads(line) for line in (root / "control" / "events.ndjson").read_text(encoding="utf-8").splitlines()]
    assert [event["seq"] for event in logs] == list(range(1, len(logs) + 1))
    assert job.completed_through == "2024-01-03"


def test_strategy_failure_preserves_only_completed_days(tmp_path):
    source = STRATEGY.replace('assert value == 1', 'if str(ctx.asof.date()) == "2024-01-03": raise RuntimeError("test failure")')
    store, run, _ = ready_run(tmp_path, source)
    job = run_until_finished(store, run.job_id)
    assert job.state == "failed"
    partial = read_partial(store.artifact_root / run.job_id / "control")
    assert partial["completed_through"] == job.completed_through == "2024-01-02"
    assert len(partial["tables"]["equity"]) == 2
    assert not (store.artifact_root / run.job_id / "final").exists()


def test_exact_rerun_uses_original_inputs_and_same_canonical_hashes(tmp_path):
    store, run, strategy = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    (strategy.path / "helper.py").write_text("value = 999")
    repeated = RunService(store).rerun(run.run_id, "rerun", "web")
    assert repeated.run_id != run.run_id and repeated.parent_run_id == run.run_id
    assert repeated.manifest_path == run.manifest_path
    assert RunService(store).rerun(run.run_id, "rerun", "web").run_id == repeated.run_id
    assert run_until_finished(store, repeated.job_id).state == "succeeded"
    first = json.loads((store.artifact_root / run.job_id / "final" / "manifest.json").read_text())
    second = json.loads((store.artifact_root / repeated.job_id / "final" / "manifest.json").read_text())
    assert first == second


def test_hash_seed_is_effective_in_actual_worker(tmp_path):
    source = STRATEGY.replace("assert value == 1", "assert hash('quanto-frozen-worker') == -5756503118753072803")
    store, run, _ = ready_run(tmp_path, source)
    assert run_until_finished(store, run.job_id).state == "succeeded"


def test_strategy_auxiliary_cannot_hijack_bootstrap_dependency(tmp_path):
    store, run, _ = ready_run(tmp_path, extra_files={"psutil.py": b"raise RuntimeError('user source before acknowledgement')"})
    assert run_until_finished(store, run.job_id).state == "succeeded"


@pytest.mark.parametrize("seed", [-1, 2**40])
def test_valid_integer_seed_runs(tmp_path, seed):
    source = STRATEGY.replace("assert initial == (0.32383276483316237, 0.07630828937395717)", "assert 0 <= initial[0] < 1 and 0 <= initial[1] < 1")
    store, run, _ = ready_run(tmp_path, source, seed=seed)
    assert run_until_finished(store, run.job_id).state == "succeeded"


def test_worker_rejects_environment_drift_even_for_non_exact_strategy(tmp_path, monkeypatch):
    installed = list(importlib.metadata.distributions())
    with monkeypatch.context() as patch:
        patch.setattr(importlib.metadata, "distributions", lambda: installed + [SimpleNamespace(metadata={"Name": "missing-distribution"}, version="1")])
        store, run, _ = ready_run(tmp_path, STRATEGY + "\nunused = lambda: open('external')\n")
    job = run_until_finished(store, run.job_id)
    assert job.state == "failed"
    control = store.artifact_root / run.job_id / "control"
    assert (control / "environment.json").exists()
    assert not (control / "runtime.json").exists()


def test_non_exact_run_executes_but_exact_rerun_is_rejected(tmp_path):
    source = STRATEGY + "\n# Explicit external-state provenance is conservatively detected.\nunused = lambda: open('external')\n"
    store, run, _ = ready_run(tmp_path, source)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    with pytest.raises(ValueError, match="not exact-reproducible"):
        RunService(store).rerun(run.run_id, "exact", "web")


def test_published_output_without_success_commit_remains_interrupted(tmp_path, monkeypatch):
    store, run, _ = ready_run(tmp_path)
    scheduler = Scheduler(store, poll_interval=0.01)
    scheduler.start(background=False)
    original = store.finish_job
    def failed_commit(identity, state, **kwargs):
        if state == "succeeded":
            raise OSError("database unavailable")
        return original(identity, state, **kwargs)
    monkeypatch.setattr(store, "finish_job", failed_commit)
    try:
        with pytest.raises(OSError, match="database unavailable"):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                scheduler.tick()
                time.sleep(0.01)
        assert (store.artifact_root / run.job_id / "final" / "report.html").exists()
    finally:
        scheduler.stop()
    Scheduler(store).recover()
    assert store.get_job(run.job_id).state == "interrupted"


def test_cancel_stuck_strategy_preserves_partial_and_reaps_actual_worker(tmp_path):
    source = STRATEGY.replace("import random", "import random\nimport time").replace("assert value == 1", 'if str(ctx.asof.date()) == "2024-01-03": time.sleep(30)')
    store, run, _ = ready_run(tmp_path, source)
    scheduler = Scheduler(store, cancel_timeout=0.05)
    scheduler.start(background=False)
    worker = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            scheduler.tick()
            job = store.get_job(run.job_id)
            if job.completed_through == "2024-01-02":
                worker = psutil.Process(job.pid)
                RunService(store).cancel(run.run_id)
                break
            time.sleep(0.01)
        assert worker is not None
        while time.monotonic() < deadline and store.get_job(run.job_id).state != "cancelled":
            scheduler.tick()
            time.sleep(0.01)
        job = store.get_job(run.job_id)
        assert job.state == "cancelled" and job.completed_through == "2024-01-02"
        assert not worker.is_running()
        assert len(read_partial(store.artifact_root / run.job_id / "control")["tables"]["equity"]) == 2
    finally:
        scheduler.stop()


def test_parent_dies_after_spawn_before_pid_commit_child_never_imports_strategy(tmp_path):
    store, run, _ = ready_run(tmp_path, "raise RuntimeError('strategy must not enter')")
    script = '''import os, sys, time
from pathlib import Path
from quanto.workbench.store import WorkbenchStore
from quanto.workbench.scheduler import Scheduler
store = WorkbenchStore(Path(sys.argv[1]))
scheduler = Scheduler(store)
scheduler.start(background=False)
scheduler.tick()
control = store.artifact_root / sys.argv[2] / 'control'
deadline = time.monotonic() + 10
while not (control / 'identity.json').exists() and time.monotonic() < deadline:
    time.sleep(0.01)
os._exit(99)
'''
    parent = subprocess.Popen([sys.executable, "-c", script, str(store.root), run.job_id],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    assert parent.wait(timeout=15) == 99
    control = store.artifact_root / run.job_id / "control"
    registered = json.loads((control / "identity.json").read_text())
    assert store.get_job(run.job_id).pid is None
    assert not (control / "runtime.json").exists() and not (control / "events.ndjson").exists()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            worker = psutil.Process(registered["pid"])
            if str(worker.create_time()) != registered["process_start"]:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.01)
    else:
        pytest.fail("unacknowledged orphan remained alive")
    Scheduler(store).recover()
    assert store.get_job(run.job_id).state == "interrupted"


def test_final_publication_failure_cannot_succeed(tmp_path):
    sabotage = '''
from pathlib import Path
original_write = Path.write_bytes
def broken_write(path, data):
    if path.name == "report.html" and path.parent.name == "staging":
        raise OSError("disk full")
    return original_write(path, data)
Path.write_bytes = broken_write
'''
    # Import-time changes exercise the actual frozen writer boundary in the child.
    source = STRATEGY + sabotage
    store, run, _ = ready_run(tmp_path, source)
    job = run_until_finished(store, run.job_id)
    assert job.state == "failed"
    assert job.completed_through == "2024-01-03"
    assert not (store.artifact_root / run.job_id / "final").exists()


def test_output_schema_and_hashes_must_both_validate(tmp_path):
    import hashlib
    from quanto.workbench.worker import validate_backtest_output
    store, run, _ = ready_run(tmp_path)
    assert run_until_finished(store, run.job_id).state == "succeeded"
    root = store.artifact_root / run.job_id / "final"
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    (root / "equity.csv").write_text("wrong,columns\n1,2\n")
    with pytest.raises(ValueError, match="INVALID_OUTPUT"):
        validate_backtest_output(root, hashes)
    hashes["equity.csv"] = hashlib.sha256((root / "equity.csv").read_bytes()).hexdigest()
    manifest = {"schema_version": "1", "files": {name: value for name, value in hashes.items() if name != "manifest.json"}}
    (root / "manifest.json").write_text(json.dumps(manifest))
    hashes["manifest.json"] = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="INVALID_OUTPUT"):
        validate_backtest_output(root, hashes)


@pytest.mark.parametrize("name", ["dataset/prices.csv", "src/quanto/__init__.py"])
def test_changed_frozen_dataset_fails_before_strategy_entry(tmp_path, name):
    store, run, _ = ready_run(tmp_path)
    (run.manifest_path.parent / name).write_text("raise RuntimeError('unverified source executed')")
    job = run_until_finished(store, run.job_id)
    assert job.state == "failed"
    assert job.pid is not None
    traceback = (store.artifact_root / run.job_id / "control" / "traceback.txt").read_text(encoding="utf-8")
    assert "SNAPSHOT_INVALID" in traceback
    assert not (store.artifact_root / run.job_id / "control" / "runtime.json").exists()


def _identity_alive(identity):
    from quanto.workbench.worker import same_process
    return same_process(identity["pid"], identity["process_start"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process-lifetime ownership")
@pytest.mark.parametrize("failure", ["service_death", "worker_exit"])
def test_owned_helpers_exit_when_service_or_worker_dies_after_ack(tmp_path, failure):
    # The helper writes its own actual PID (not the Windows venv redirector PID).
    helper_code = """import json, os, sys, time
from pathlib import Path
import psutil
path = Path(sys.argv[1])
temporary = path.with_suffix('.tmp')
temporary.write_text(json.dumps({'pid': os.getpid(), 'process_start': str(psutil.Process().create_time())}))
temporary.replace(path)
time.sleep(30)
"""
    addition = f'''
import os, subprocess, sys, time
from pathlib import Path
helper = subprocess.Popen([sys.executable, "-c", {helper_code!r}, str(Path.cwd() / "helper.json")], creationflags=subprocess.CREATE_NO_WINDOW)
deadline = time.monotonic() + 10
while not (Path.cwd() / "helper.json").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if {failure!r} == "worker_exit":
    os._exit(77)
time.sleep(30)
'''
    store, run, _ = ready_run(tmp_path, STRATEGY + addition)
    script = '''import os, sys, time
from pathlib import Path
from quanto.workbench.store import WorkbenchStore
from quanto.workbench.scheduler import Scheduler
store = WorkbenchStore(Path(sys.argv[1]))
scheduler = Scheduler(store)
scheduler.start(background=False)
control = store.artifact_root / sys.argv[2] / 'control'
deadline = time.monotonic() + 12
while time.monotonic() < deadline:
    scheduler.tick()
    if sys.argv[3] == 'service_death' and (control / 'helper.json').exists():
        os._exit(99)
    if store.get_job(sys.argv[2]).state == 'failed':
        (control / 'failure-observed').touch()
        while not (control / 'release-parent').exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        scheduler.stop()
        sys.exit(0)
    time.sleep(0.01)
scheduler.stop()
sys.exit(3)
'''
    parent = subprocess.Popen([sys.executable, "-c", script, str(store.root), run.job_id, failure], creationflags=subprocess.CREATE_NO_WINDOW)
    parent_identity = {"pid": parent.pid, "process_start": str(psutil.Process(parent.pid).create_time())}
    control = store.artifact_root / run.job_id / "control"
    identities = []
    try:
        if failure == "service_death":
            assert parent.wait(timeout=15) == 99
        else:
            deadline = time.monotonic() + 10
            while not (control / "failure-observed").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (control / "failure-observed").exists()
            assert parent.poll() is None
        assert (control / "ack.json").exists()
        identities = [json.loads((control / name).read_text()) for name in ("identity.json", "helper.json")]
        deadline = time.monotonic() + 3
        while any(_identity_alive(identity) for identity in identities) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(_identity_alive(identity) for identity in identities), "owned worker/helper survived root exit"
        if failure == "worker_exit":
            (control / "release-parent").touch()
            assert parent.wait(timeout=5) == 0
        Scheduler(store).recover()
        assert store.get_job(run.job_id).state == ("interrupted" if failure == "service_death" else "failed")
        assert not any(_identity_alive(identity) for identity in identities)
    finally:
        if control.exists():
            (control / "release-parent").touch()
        if parent.poll() is None:
            Scheduler._terminate_registered(parent_identity)
            parent.wait(timeout=5)
        for name in ("identity.json", "helper.json", "launcher.json"):
            if (control / name).exists():
                Scheduler._terminate_registered(json.loads((control / name).read_text()))
