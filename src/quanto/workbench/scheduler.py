from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading
import time

import psutil

from quanto.workbench.models import JobRecord
from quanto.workbench.store import WorkbenchStore
from quanto.workbench.worker import atomic_json, process_identity, read_json, read_completed_through, same_process, validate_job_output, verify_bootstrap


@dataclass
class _Child:
    process: subprocess.Popen
    launched: float
    cancel_at: float | None = None
    acknowledged: bool = False
    worker: psutil.Process | None = None
    launcher_start: str | None = None


class Scheduler:
    def __init__(self, store: WorkbenchStore, max_workers: int = 1, *, poll_interval=0.1,
                 handshake_timeout=10.0, heartbeat_timeout=10.0, cancel_timeout=2.0,
                 clock=time.monotonic, launcher=subprocess.Popen):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.store = store
        self.max_workers = max_workers
        self.poll_interval = poll_interval
        self.handshake_timeout = handshake_timeout
        self.heartbeat_timeout = heartbeat_timeout
        self.cancel_timeout = cancel_timeout
        self.clock = clock
        self.launcher = launcher
        self._active: dict[str, _Child] = {}
        self._lock = None
        self._thread = None
        self._stopped = threading.Event()
        self.error: BaseException | None = None

    def _acquire(self):
        stream = (self.store.root / "scheduler.lock").open("a+b")
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("SCHEDULER_LOCKED: 已有调度服务运行") from exc
        self._lock = stream

    def _release(self):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def start(self, *, background=True):
        if self._lock is not None:
            return
        self._acquire()
        try:
            self.recover()
        except BaseException:
            self._release()
            raise
        self._stopped.clear()
        if background:
            self._thread = threading.Thread(target=self._loop, name="quanto-scheduler", daemon=True)
            self._thread.start()

    def _loop(self):
        try:
            while not self._stopped.is_set():
                self.tick()
                self._stopped.wait(self.poll_interval)
        except BaseException as exc:
            self.error = exc
            self._stopped.set()
            try:
                self._reap_owned()
            except BaseException as cleanup_error:
                self.error = cleanup_error
            finally:
                self._release()

    def _control(self, job: JobRecord) -> Path:
        return job.artifact_root / job.id / "control"

    def recover(self):
        acquired = self._lock is None
        if acquired:
            self._acquire()
        try:
            for job in self.store.list_jobs():
                if job.state not in {"running", "cancel_requested"}:
                    continue
                control = self._control(job)
                identity = {"pid": job.pid, "process_start": job.process_start}
                if job.pid is None and (control / "identity.json").exists():
                    registered = read_json(control / "identity.json")
                    if registered.get("job_id") == job.id:
                        identity = registered
                error = "SERVICE_RESTARTED: 服务重启，任务已中断"
                identities = [identity]
                if (control / "launcher.json").exists():
                    launcher = read_json(control / "launcher.json")
                    if launcher.get("job_id") == job.id:
                        identities.append(launcher)
                for registered in identities:
                    if registered["pid"] is None or registered["process_start"] is None:
                        continue
                    if not self._terminate_registered(registered):
                        error = "PROCESS_IDENTITY_CHANGED: 原进程已退出或标识变化；未终止其他进程"
                self._checkpoint(job)
                self.store.finish_job(job.id, "interrupted", expected=job.state, error=error)
        finally:
            if acquired:
                self._release()

    @staticmethod
    def _terminate_registered(identity: dict) -> bool:
        try:
            process = psutil.Process(identity["pid"])
            if str(process.create_time()) != identity["process_start"]:
                return False
            # psutil rechecks creation identity when signaling each Process object.
            for child in process.children(recursive=True):
                try:
                    child.kill()
                    child.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
            process.kill()
            process.wait(timeout=5)
            return True
        except psutil.NoSuchProcess:
            return False

    def _launch(self, job: JobRecord):
        control = self._control(job)
        control.mkdir(parents=True, exist_ok=False)
        descriptor = {"job_id": job.id, "kind": job.kind, "run_id": job.run_id,
            "input_path": job.input_path, "parent_pid": os.getpid(), "parent_start": process_identity(os.getpid()),
            "handshake_timeout": self.handshake_timeout, "heartbeat_interval": min(0.2, self.heartbeat_timeout / 4),
            "site_paths": sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})}
        seed = 0
        if job.kind == "backtest":
            run = self.store.get_run(job.run_id)
            descriptor.update(manifest_path=str(run.manifest_path), manifest_hash=run.manifest_hash, source_root=str(run.manifest_path.parent))
            manifest = verify_bootstrap(descriptor)
            seed = manifest["seed"]
        else:
            # A T7 submission freezes its own kind/input descriptor with source_root.
            frozen = read_json(Path(job.input_path))
            descriptor.update(source_root=frozen["source_root"], input=frozen["input"])
        path = control / "job.json"
        atomic_json(path, descriptor)
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED=str(seed % 2**32))
        worker = Path(descriptor["source_root"]) / "src" / "quanto" / "workbench" / "worker.py"
        # -P excludes cwd/script imports; -S excludes site hooks/editable .pth files.
        # Unlike -I, these preserve the pre-start hash seed from the sanitized env.
        with (control / "process.log").open("wb") as log:
            process = self.launcher([sys.executable, "-P", "-B", "-S", str(worker), str(path), str(control)],
                cwd=control, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        child = _Child(process, self.clock())
        self._active[job.id] = child
        child.launcher_start = process_identity(process.pid)
        atomic_json(control / "launcher.json", {"job_id": job.id, "pid": process.pid, "process_start": child.launcher_start})

    def _checkpoint(self, job: JobRecord):
        control = self._control(job)
        heartbeat = read_json(control / "heartbeat.json") if (control / "heartbeat.json").exists() else None
        # The SQLite commit is the authority even if death interrupted log/marker write.
        completed = read_completed_through(control)
        self.store.checkpoint(job.id, heartbeat["at"] if heartbeat and heartbeat.get("job_id") == job.id else None, completed)

    @staticmethod
    def _kill(child: _Child):
        # Windows venv python.exe may be a redirector owning the real worker.
        # Capture verified descendants before terminating that retained launcher.
        if child.launcher_start is not None and same_process(child.process.pid, child.launcher_start):
            try:
                launcher = psutil.Process(child.process.pid)
                if str(launcher.create_time()) == child.launcher_start:
                    for descendant in launcher.children(recursive=True):
                        try:
                            descendant.kill()
                            descendant.wait(timeout=5)
                        except psutil.NoSuchProcess:
                            pass
            except psutil.NoSuchProcess:
                pass
        if child.worker is not None:
            try:
                child.worker.kill()
                child.worker.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        if child.process.poll() is None:
            child.process.kill()  # Retained OS process handle, not a reconstructed PID.
        child.process.wait(timeout=5)

    def _finish_exited(self, job: JobRecord, child: _Child):
        control = self._control(job)
        # A redirector can exit while its registered Python child is still alive.
        registration = control / "identity.json"
        if registration.exists():
            identity = read_json(registration)
            if identity.get("job_id") == job.id:
                self._terminate_registered(identity)
        self._kill(child)
        self._checkpoint(job)
        receipt = read_json(control / "receipt.json") if (control / "receipt.json").exists() else {}
        if job.state == "cancel_requested":
            self.store.finish_job(job.id, "cancelled", expected="cancel_requested")
        else:
            valid = child.process.returncode == 0 and receipt.get("job_id") == job.id and receipt.get("state") == "succeeded"
            error = receipt.get("error", "WORKER_EXITED: 工作进程异常退出")
            if valid:
                try:
                    validate_job_output(job.kind, control.parent, receipt)
                except (OSError, ValueError, KeyError):
                    valid = False
                    error = "INVALID_OUTPUT: 最终产物校验失败"
            if not self.store.finish_job(job.id, "succeeded" if valid else "failed", expected="running", error=None if valid else error):
                # Cancel committed after the read above, and the child is already gone.
                self.store.finish_job(job.id, "cancelled", expected="cancel_requested")
        del self._active[job.id]

    def tick(self):
        if self._lock is None:
            raise RuntimeError("scheduler must be started")
        for identity, child in list(self._active.items()):
            job = self.store.get_job(identity)
            control = self._control(job)
            if child.process.poll() is not None:
                self._finish_exited(job, child)
                continue
            self._checkpoint(job)
            if not child.acknowledged and (control / "identity.json").exists():
                registration = read_json(control / "identity.json")
                try:
                    candidate = psutil.Process(registration["pid"])
                    ancestors = {parent.pid: str(parent.create_time()) for parent in candidate.parents()}
                except psutil.NoSuchProcess:
                    continue
                owned = (candidate.pid == child.process.pid and str(candidate.create_time()) == child.launcher_start) or ancestors.get(child.process.pid) == child.launcher_start
                if registration.get("job_id") != identity or not owned or str(candidate.create_time()) != registration["process_start"]:
                    self._kill(child)
                    continue
                child.worker = candidate
                if self.store.register_process(identity, registration["pid"], registration["process_start"]):
                    atomic_json(control / "ack.json", {"job_id": identity})
                    child.acknowledged = True
            now = self.clock()
            if job.state == "cancel_requested":
                (control / "cancel").touch(exist_ok=True)
                if child.cancel_at is None:
                    child.cancel_at = now
                if now - child.cancel_at >= self.cancel_timeout:
                    self._kill(child)
            elif not child.acknowledged and now - child.launched >= self.handshake_timeout:
                self._kill(child)
            elif child.acknowledged:
                heartbeat = read_json(control / "heartbeat.json") if (control / "heartbeat.json").exists() else {}
                if heartbeat.get("job_id") != identity or now - heartbeat.get("monotonic", child.launched) >= self.heartbeat_timeout:
                    self._kill(child)
        while len(self._active) < self.max_workers and not self._stopped.is_set():
            job = self.store.claim_job()
            if job is None:
                break
            try:
                self._launch(job)
            except Exception:
                if job.id in self._active:
                    self._kill(self._active.pop(job.id))
                if not self.store.finish_job(job.id, "interrupted", expected="running", error="LAUNCH_FAILED: 工作进程启动失败"):
                    self.store.finish_job(job.id, "cancelled", expected="cancel_requested")
                break

    def _reap_owned(self):
        error = None
        for identity, child in list(self._active.items()):
            try:
                self._kill(child)
                job = self.store.get_job(identity)
                self._checkpoint(job)
                if job.state in {"running", "cancel_requested"}:
                    self.store.finish_job(identity, "interrupted", expected=job.state, error="SERVICE_STOPPED: 服务已停止，任务已中断")
                del self._active[identity]
            except BaseException as exc:
                error = error or exc
        if error:
            raise error

    def stop(self):
        self._stopped.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        try:
            self._reap_owned()
        finally:
            self._release()
