from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quanto.data.contracts import BAR_ORDER, FragmentRef, MarketDataError, NormalizedChunk, SnapshotManifest, SnapshotRef, content_key
from quanto.objects import digest, file_hash, json_bytes, safe_name, verified_files


class MarketStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self._path("staging").mkdir(exist_ok=True)

    def _path(self, name: str) -> Path:
        safe_name(name)
        target = self.root / name
        if any(path.is_symlink() for path in (target, *target.parents)):
            raise MarketDataError("INTEGRITY_ERROR", "symbolic link in market path")
        return target

    def object_path(self, ref: FragmentRef) -> Path:
        return self._path(f"objects/{ref.object_hash[:2]}/{ref.object_hash}.parquet")

    def _publish(self, stage: Path, name: str) -> Path:
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(stage, target)
        except FileExistsError:
            if not target.is_file() or file_hash(target) != file_hash(stage):
                raise MarketDataError("INTEGRITY_ERROR", "existing immutable object differs")
        return target

    def put_chunk(self, chunk: NormalizedChunk) -> tuple[FragmentRef, ...]:
        evidence_hash = digest(chunk.evidence)
        source = chunk.source.model_copy(update={"evidence_hash": evidence_hash})
        frames = [chunk.frame]
        if chunk.request.frequency == "1m" and not chunk.frame.empty:
            months = pd.to_datetime(chunk.frame.bar_start, utc=True).dt.strftime("%Y-%m")
            frames = [group for _, group in chunk.frame.groupby(months, sort=True)]
        result = []
        with tempfile.TemporaryDirectory(dir=self._path("staging")) as stage_name:
            stage = Path(stage_name)
            evidence = stage / "evidence.bin"
            evidence.write_bytes(chunk.evidence)
            self._publish(evidence, f"evidence/{evidence_hash[:2]}/{evidence_hash}.bin")
            for index, frame in enumerate(frames):
                order = [column for column in BAR_ORDER if column in frame]
                if not order:
                    order = list(frame.columns)
                frame = frame.sort_values(order, kind="stable").reset_index(drop=True)
                frame = frame.reindex(sorted(frame.columns), axis=1)
                path = stage / f"{index}.parquet"
                pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), path, compression="zstd")
                identity = file_hash(path)
                self._publish(path, f"objects/{identity[:2]}/{identity}.parquet")
                request = chunk.request
                if request.frequency == "1m" and not frame.empty:
                    first = pd.Timestamp(frame.bar_start.min()).to_pydatetime()
                    month_start = first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    next_month = (pd.Timestamp(month_start) + pd.offsets.MonthBegin()).to_pydatetime()
                    request = request.model_copy(update={"start": max(request.start, month_start), "end": min(request.end, next_month)})
                result.append(FragmentRef(object_hash=identity, request=request, source=source, source_evidence_hash=evidence_hash, row_count=len(frame)))
        return tuple(result)

    def write_manifest(self, manifest: SnapshotManifest) -> SnapshotRef:
        requests = tuple(sorted(manifest.requirement.requests, key=lambda item: content_key(item.model_dump(mode="json"))))
        entries = tuple(sorted(manifest.coverage.entries, key=lambda item: content_key(item.request.model_dump(mode="json"))))
        manifest = manifest.model_copy(update={"requirement": manifest.requirement.model_copy(update={"requests": requests}),
            "fragments": tuple(sorted(manifest.fragments, key=lambda item: content_key(item.model_dump(mode="json")))),
            "coverage": manifest.coverage.model_copy(update={"entries": entries})})
        data = json_bytes(manifest.model_dump(mode="json"))
        identity = digest(data)
        with tempfile.TemporaryDirectory(dir=self._path("staging")) as stage_name:
            stage = Path(stage_name) / "manifest.json"
            stage.write_bytes(data)
            self._publish(stage, f"snapshots/{identity}/manifest.json")
        ref = SnapshotRef(id=identity, manifest_digest=identity, coverage=manifest.coverage, quality=manifest.quality)
        self.verify_snapshot(ref)
        return ref

    def verify_snapshot(self, ref: SnapshotRef) -> SnapshotManifest:
        try:
            root = self._path(f"snapshots/{ref.id}")
            verified_files(root, {"manifest.json": ref.manifest_digest})
            data = (root / "manifest.json").read_bytes()
            if digest(data) != ref.id:
                raise ValueError("snapshot identity mismatch")
            manifest = SnapshotManifest.model_validate_json(data)
            for fragment in manifest.fragments:
                path = self.object_path(fragment)
                evidence = self._path(f"evidence/{fragment.source_evidence_hash[:2]}/{fragment.source_evidence_hash}.bin")
                if not path.is_file() or file_hash(path) != fragment.object_hash or not evidence.is_file() or file_hash(evidence) != fragment.source_evidence_hash:
                    raise ValueError("market fragment or evidence hash mismatch")
            return manifest
        except (ValueError, OSError) as exc:
            raise MarketDataError("INTEGRITY_ERROR", str(exc)) from exc
