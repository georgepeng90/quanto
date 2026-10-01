from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
import threading
from uuid import uuid4

from quanto.data.acquisition import AcquisitionCoordinator, MarketConfig
from quanto.data.catalog import MarketCatalog
from quanto.data.contracts import DataQuery, DataRequirement, MarketDataError, PrefetchStatus, PrefetchTaskRef, SnapshotRef, TableQuery
from quanto.data.coverage import evaluate_coverage
from quanto.data.readers import MarketReader
from quanto.data.storage import MarketStore


@dataclass
class _Task:
    status: PrefetchStatus = field(default_factory=lambda: PrefetchStatus(state="queued"))
    completion: Future = field(default_factory=Future)


@dataclass
class _Work:
    requirement: DataRequirement
    refresh: bool
    tasks: list[str] = field(default_factory=list)
    finished: bool = False


class MarketData:
    def __init__(self, root: Path, config: MarketConfig | None = None, providers=None):
        self.config = config or MarketConfig()
        self.store, self.catalog = MarketStore(root), MarketCatalog(root)
        self.reader = MarketReader(self.store)
        self._providers = providers
        self._workers = ThreadPoolExecutor(max_workers=self.config.max_workers, thread_name_prefix="market-prefetch")
        self._io = ThreadPoolExecutor(max_workers=self.config.max_workers, thread_name_prefix="market-local")
        self._lock = threading.RLock()
        self._tasks, self._work = {}, {}
        self._closed = False

    def _requirement(self, requirement):
        return DataRequirement.model_validate(requirement.model_dump() if isinstance(requirement, DataRequirement) else requirement)

    def _task(self, identity):
        try:
            return self._tasks[identity]
        except KeyError as exc:
            raise MarketDataError("TASK_NOT_FOUND", "task belongs to another instance or does not exist") from exc

    def coverage(self, requirement):
        requirement = self._requirement(requirement)
        return evaluate_coverage(requirement, self.catalog.fragments(requirement), self.store)

    def prefetch(self, requirement, refresh=False):
        requirement = self._requirement(requirement)
        with self._lock:
            if self._closed:
                raise MarketDataError("CLOSED", "market instance is closed")
            identity = uuid4().hex
            self.catalog.create_request(identity, requirement)
            self._tasks[identity] = _Task()
            key = (requirement.key, bool(refresh))
            work = self._work.get(key)
            if work is None or work.finished:
                work = _Work(requirement, bool(refresh), [identity])
                self._work[key] = work
                self._workers.submit(self._run, work)
            else:
                work.tasks.append(identity)
            return PrefetchTaskRef(id=identity)

    def _active(self, work):
        with self._lock:
            return tuple(identity for identity in work.tasks if self._tasks[identity].status.state != "cancelled")

    def _run(self, work):
        def progress(report):
            with self._lock:
                fraction = sum(entry.complete for entry in report.entries) / max(1, len(report.entries))
                for identity in work.tasks:
                    task = self._tasks[identity]
                    if not task.completion.done():
                        task.status = PrefetchStatus(state="running", progress=fraction, coverage=report, limitations=report.limitations)
        with self._lock:
            for identity in work.tasks:
                if not self._tasks[identity].completion.done():
                    self._tasks[identity].status = PrefetchStatus(state="running")
        try:
            providers = self._providers
            if providers is None:
                # Creating adapters does not import the optional SDK or access a source.
                from quanto.data.providers.akshare import AKShareProvider
                from quanto.data.providers.tencent import TencentProvider
                providers = (AKShareProvider(), TencentProvider())
            engine = AcquisitionCoordinator(self.store, self.catalog, providers, self.config)
            ref = engine.prepare(work.requirement, work.refresh, lambda: self._active(work), progress, lambda: not self._active(work))
            result = PrefetchStatus(state="ready", progress=1, coverage=ref.coverage, snapshot=ref, limitations=ref.coverage.limitations)
        except Exception as exc:
            if isinstance(exc, MarketDataError):
                state = "incomplete" if exc.code in {"INCOMPLETE", "CAPABILITY_UNAVAILABLE", "SEMANTIC_CONFLICT"} else "cancelled" if exc.code == "CANCELLED" else "failed"
                result = PrefetchStatus(state=state, reason=str(exc), coverage=exc.coverage or self._tasks[work.tasks[0]].status.coverage)
            else:
                result = PrefetchStatus(state="failed", reason=f"{type(exc).__name__}: {exc}")
        with self._lock:
            work.finished = True
            for identity in work.tasks:
                task = self._tasks[identity]
                if task.completion.done():
                    continue
                try:
                    if result.state == "ready":
                        # A consumer may have joined after the coordinator took its publication list.
                        committed = self.catalog.request_snapshot(identity)
                        if committed is None:
                            eligible = self.catalog.commit_ready((identity,), result.snapshot, ())
                            if not eligible:
                                self._finish(task, PrefetchStatus(state="cancelled", reason="request cancelled before binding ready snapshot"))
                                continue
                    self._finish(task, result)
                except Exception as exc:
                    self._finish(task, PrefetchStatus(state="failed", reason=f"snapshot binding failed: {exc}"))

    def _finish(self, task, status):
        task.status = status
        if not task.completion.done():
            task.completion.set_result(status)

    def get_prefetch(self, task_id):
        with self._lock:
            return self._task(task_id).status.model_copy(deep=True)

    def _ready(self, status):
        if status.state == "ready":
            return status.snapshot.model_copy(deep=True)
        raise MarketDataError(status.state.upper(), status.reason or "preparation did not complete", status.coverage)

    def wait_ready(self, task_id, timeout=None):
        with self._lock:
            completion = self._task(task_id).completion
        try:
            return self._ready(completion.result(timeout=timeout))
        except TimeoutError as exc:
            raise MarketDataError("WAIT_TIMEOUT", "wait ended; prefetch remains active", self.get_prefetch(task_id).coverage) from exc

    def cancel_prefetch(self, task_id):
        with self._lock:
            task = self._task(task_id)
            if task.completion.done():
                return
            self.catalog.cancel_request(task_id)
            ref = self.catalog.request_snapshot(task_id)
            if ref:
                self._finish(task, PrefetchStatus(state="ready", progress=1, coverage=ref.coverage, snapshot=ref, limitations=ref.coverage.limitations))
            else:
                self._finish(task, PrefetchStatus(state="cancelled", coverage=task.status.coverage))

    def _snapshot(self, snapshot):
        snapshot = SnapshotRef.model_validate(snapshot.model_dump() if isinstance(snapshot, SnapshotRef) else snapshot)
        committed = self.catalog.resolve_snapshot(snapshot.id)
        if committed.manifest_digest != snapshot.manifest_digest:
            raise MarketDataError("INTEGRITY_ERROR", "snapshot digest differs from catalog")
        return committed

    def resolve_snapshot(self, identity):
        ref = self.catalog.resolve_snapshot(identity)
        self.store.verify_snapshot(ref)
        return ref.model_copy(deep=True)

    async def resolve_snapshot_async(self, identity):
        return await self._async(self.resolve_snapshot, identity)

    def read_bars(self, snapshot, query):
        query = DataQuery.model_validate(query.model_dump())
        return self.reader.read_bars(self._snapshot(snapshot), query)

    def read_table(self, snapshot, table, query):
        query = TableQuery.model_validate(query.model_dump())
        return self.reader.read_table(self._snapshot(snapshot), table, query)

    def iter_batches(self, snapshot, query):
        query = DataQuery.model_validate(query.model_dump())
        return self.reader.iter_batches(self._snapshot(snapshot), query, self.config.batch_size)

    async def _async(self, function, *args):
        return await asyncio.wrap_future(self._io.submit(function, *args))

    async def coverage_async(self, requirement):
        return await self._async(self.coverage, requirement)

    async def prefetch_async(self, requirement, refresh=False):
        return await self._async(self.prefetch, requirement, refresh)

    async def get_prefetch_async(self, task_id):
        return await self._async(self.get_prefetch, task_id)

    async def cancel_prefetch_async(self, task_id):
        return await self._async(self.cancel_prefetch, task_id)

    async def wait_ready_async(self, task_id, timeout=None):
        with self._lock:
            completion = self._task(task_id).completion
        try:
            status = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(completion)), timeout)
        except asyncio.TimeoutError as exc:
            raise MarketDataError("WAIT_TIMEOUT", "wait ended; prefetch remains active") from exc
        return self._ready(status)

    async def read_bars_async(self, snapshot, query):
        return await self._async(self.read_bars, snapshot, query)

    async def read_table_async(self, snapshot, table, query):
        return await self._async(self.read_table, snapshot, table, query)

    async def iter_batches_async(self, snapshot, query):
        iterator = await self._async(self.iter_batches, snapshot, query)
        pending = None
        try:
            while True:
                pending = asyncio.create_task(self._async(next, iterator, None))
                batch = await asyncio.shield(pending)
                if batch is None:
                    break
                yield batch
        finally:
            if pending:
                await pending
            iterator.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for identity in self._tasks:
                self.cancel_prefetch(identity)
        self._workers.shutdown(wait=True)
        self._io.shutdown(wait=True)

    async def close_async(self):
        await asyncio.to_thread(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close_async()
