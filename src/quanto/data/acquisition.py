from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from uuid import uuid4

import pandas as pd

from quanto.data.catalog import compatible
from quanto.data.contracts import BAR_KEY, DataRequirement, MarketDataError, NormalizedChunk, SnapshotManifest, SourceRecord
from quanto.data.coverage import evaluate_coverage, fragment_frame
from quanto.data.providers.base import FetchContext
from quanto.data.validation import deduplicate, validate_chunk
from quanto.objects import json_bytes


@dataclass(frozen=True)
class MarketConfig:
    source_priority: tuple[str, ...] = ("akshare", "tencent")
    request_timeout_seconds: float = 30
    max_retries: int = 2
    retry_delay_seconds: float = 1
    max_workers: int = 4
    batch_size: int = 65536

    def __post_init__(self):
        if self.request_timeout_seconds <= 0 or self.max_retries < 0 or self.retry_delay_seconds < 0 or self.max_workers <= 0 or self.batch_size <= 0:
            raise ValueError("invalid market execution limits")


class AcquisitionCoordinator:
    def __init__(self, store, catalog, providers, config):
        self.store, self.catalog, self.config = store, catalog, config
        self.providers = tuple(sorted(providers, key=lambda item: config.source_priority.index(item.name) if item.name in config.source_priority else len(config.source_priority)))

    def _coverage(self, requirement):
        refs = self.catalog.fragments(requirement)
        return refs, evaluate_coverage(requirement, refs, self.store)

    def _fetch(self, request, cancelled):
        available = [provider for provider in self.providers if provider.capabilities().supports(request)]
        if not available:
            raise MarketDataError("CAPABILITY_UNAVAILABLE", "no source supports required data")
        last = None
        for provider in available:
            for attempt in range(self.config.max_retries + 1):
                if cancelled():
                    raise MarketDataError("CANCELLED", "request cancelled before source call")
                try:
                    context = FetchContext(time.monotonic() + self.config.request_timeout_seconds, cancelled)
                    chunk = provider.fetch(request, context)
                    context.remaining()
                    return validate_chunk(chunk)
                except MarketDataError as exc:
                    if exc.code in {"CANCELLED", "SEMANTIC_CONFLICT"}:
                        raise
                    last = exc
                except (OSError, ValueError, RuntimeError) as exc:
                    last = MarketDataError("FAILED", str(exc))
                if attempt < self.config.max_retries:
                    deadline = time.monotonic() + self.config.retry_delay_seconds
                    while time.monotonic() < deadline:
                        if cancelled():
                            raise MarketDataError("CANCELLED", "request cancelled during retry")
                        threading.Event().wait(min(.05, max(0, deadline - time.monotonic())))
        raise last

    def _consolidate(self, chunk, previous):
        if not previous or chunk.request.kind != "bars":
            return chunk
        if any(ref.request.price_basis != chunk.request.price_basis or ref.source.units != chunk.source.units for ref in previous):
            raise MarketDataError("SEMANTIC_CONFLICT", "incompatible price basis or units cannot be spliced")
        old_frames = [fragment_frame(self.store, ref) for ref in previous]
        combined = deduplicate(pd.concat([*old_frames, chunk.frame], ignore_index=True), BAR_KEY)
        request = chunk.request.model_copy(update={"start": min([chunk.request.start, *(ref.request.start for ref in previous)]),
            "end": max([chunk.request.end, *(ref.request.end for ref in previous)])})
        inputs = []
        for ref in previous:
            inputs.extend(ref.source.parameters.get("merge_inputs", [{"source": ref.source.model_dump(mode="json"), "request": ref.request.model_dump(mode="json")}]))
        inputs.append({"source": chunk.source.model_dump(mode="json"), "request": chunk.request.model_dump(mode="json")})
        # The new source evidence has not yet been persisted; retain its exact bytes alongside the merge record.
        from quanto.objects import digest
        inputs[-1]["source"]["evidence_hash"] = digest(chunk.evidence)
        self.store.put_chunk(chunk)
        unique = {json_bytes(item): item for item in inputs}
        inputs = [unique[key] for key in sorted(unique)]
        source = SourceRecord(provider="normalized_merge", interface="market_consolidation", upstream="declared_input_sources",
            downloaded_at=max([chunk.source.downloaded_at, *(ref.source.downloaded_at for ref in previous)]), actual_mode=chunk.source.actual_mode,
            parameters={"merge_inputs": inputs}, units=chunk.source.units,
            verified=tuple(sorted(set(chunk.source.verified).intersection(*(set(ref.source.verified) for ref in previous)))),
            limitations=tuple(sorted(set(chunk.source.limitations).union(*(ref.source.limitations for ref in previous)))))
        return validate_chunk(NormalizedChunk(request, combined, source, json_bytes({"merge_inputs": inputs})))

    def prepare(self, requirement: DataRequirement, refresh: bool, request_ids, progress, cancelled):
        refs, report = self._coverage(requirement)
        progress(report)
        owner, leases, stopped, lost = uuid4().hex, [], threading.Event(), threading.Event()
        ttl = max(5., self.config.request_timeout_seconds * 2)
        def check():
            if lost.is_set():
                raise MarketDataError("FAILED", "lease ownership lost")
            if cancelled():
                raise MarketDataError("CANCELLED", "all consumers cancelled")
        def renew():
            while not stopped.wait(ttl / 3):
                try:
                    for token in leases:
                        self.catalog.renew_lease(token, ttl)
                except (MarketDataError, OSError):
                    lost.set()
                    return
        renewal = None
        try:
            if refresh or not report.complete:
                keys = set()
                for request in requirement.requests:
                    parts = [request.model_copy(update={"instruments": (instrument,)}) for instrument in request.instruments] if request.instruments else [request]
                    keys.update(part.series_key for part in parts)
                for identity in sorted(keys):
                    while True:
                        check()
                        token = self.catalog.acquire_lease(identity, owner, ttl)
                        if token:
                            leases.append(token)
                            if renewal is None:
                                renewal = threading.Thread(target=renew, daemon=True)
                                renewal.start()
                            break
                        stopped.wait(.05)
                # A different process may have filled the gaps while we waited.
                refs, report = self._coverage(requirement)
                requests = sorted(requirement.requests, key=lambda item: item.kind == "bars")
                for request in requests:
                    check()
                    refs, report = self._coverage(requirement)
                    entry = next(item for item in report.entries if item.request == request)
                    if entry.complete and not refresh:
                        continue
                    if entry.reason == "SEMANTIC_CONFLICT" and not refresh:
                        continue
                    parts = [request.model_copy(update={"instruments": (item,)}) for item in request.instruments] if request.kind == "bars" else [request]
                    for part in parts:
                        check()
                        existing = tuple(ref for ref in self.catalog.fragments(DataRequirement(requests=(part,), quality=requirement.quality)) if compatible(ref.request, part))
                        fetch_request = part
                        gaps = entry.missing_by_instrument.get(part.instruments[0].key, ()) if part.kind == "bars" else ()
                        full_history = refresh or bool(entry.missing_fields or entry.missing_evidence) or (part.price_basis and part.price_basis.mode != "raw")
                        if full_history and existing:
                            fetch_request = part.model_copy(update={"start": min([part.start, *(ref.request.start for ref in existing)]),
                                "end": max([part.end, *(ref.request.end for ref in existing)])})
                        elif gaps and not (entry.missing_fields or entry.missing_evidence or entry.reason):
                            fetch_request = part.model_copy(update={"start": min(gaps), "end": max(gaps) + (pd.Timedelta(minutes=1) if part.frequency == "1m" else pd.Timedelta(hours=5, minutes=30))})
                        elif part.kind == "bars" and not gaps and entry.missing_by_instrument and not (entry.missing_fields or entry.missing_evidence or entry.reason):
                            continue
                        windows = [fetch_request]
                        if gaps and not full_history and not (entry.missing_fields or entry.missing_evidence or entry.reason):
                            windows = []
                            stride = pd.Timedelta(minutes=1) if part.frequency == '1m' else pd.Timedelta(days=1)
                            duration = pd.Timedelta(minutes=1) if part.frequency == '1m' else pd.Timedelta(hours=5, minutes=30)
                            begin = previous_gap = gaps[0]
                            for gap in (*gaps[1:], None):
                                if gap is None or gap != previous_gap + stride:
                                    windows.append(part.model_copy(update={'start': begin, 'end': previous_gap + duration}))
                                    begin = gap
                                previous_gap = gap
                        for fetch_request in windows:
                            existing = self.catalog.fragments(DataRequirement(requests=(part,), quality=requirement.quality))
                            try:
                                chunk = self._fetch(fetch_request, cancelled)
                            except MarketDataError as exc:
                                if exc.code in {"INCOMPLETE", "CAPABILITY_UNAVAILABLE", "SEMANTIC_CONFLICT"}:
                                    if refresh:
                                        failed = report.model_copy(update={"entries": tuple(item.model_copy(update={"reason": exc.code}) if item.request == request else item for item in report.entries)})
                                        raise MarketDataError("INCOMPLETE", "refresh could not fulfill request", failed) from exc
                                    continue
                                raise
                            check()
                            if existing and not refresh and not full_history:
                                chunk = self._consolidate(chunk, existing)
                            elif existing and not refresh and any(ref.request.price_basis != chunk.request.price_basis for ref in existing):
                                raise MarketDataError("INCOMPLETE", "incompatible source basis", report)
                            new = self.store.put_chunk(chunk)
                            check()
                            if refresh or full_history:
                                references = tuple(ref for ref in refs if ref.request.kind != "bars")
                                candidate = evaluate_coverage(DataRequirement(requests=(fetch_request,), quality=requirement.quality), new + references, self.store)
                                if not candidate.complete:
                                    if refresh:
                                        raise MarketDataError("INCOMPLETE", "refresh response is incomplete; previous cache retained", candidate)
                                    # Valid partial data may supplement the cache, but cannot replace complete historical observations.
                                    self.catalog.publish_fragments((), new, tuple(leases), request_ids())
                                    continue
                            previous = existing if part.kind == "bars" or refresh else ()
                            self.catalog.publish_fragments(previous, new, tuple(leases), request_ids())
                    refs, report = self._coverage(requirement)
                    progress(report)
            refs, report = self._coverage(requirement)
            if not report.complete:
                raise MarketDataError("INCOMPLETE", "required coverage or evidence missing", report)
            check()
            ref = self.store.write_manifest(SnapshotManifest(requirement=requirement, fragments=refs, coverage=report,
                quality={"limitations": list(report.limitations)}))
            check()
            if not self.catalog.commit_ready(request_ids(), ref, tuple(leases)):
                raise MarketDataError("CANCELLED", "no eligible request remains")
            return ref
        except OSError as exc:
            raise MarketDataError("FAILED", str(exc)) from exc
        finally:
            stopped.set()
            if renewal:
                renewal.join()
            for token in leases:
                self.catalog.release_lease(token)
