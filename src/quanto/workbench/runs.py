from __future__ import annotations

import json
from uuid import uuid4

from quanto.workbench.models import BatchItem, FrozenRun, RunRecord
from quanto.workbench.revisions import digest, json_bytes
from quanto.workbench.snapshots import SnapshotBuilder
from quanto.workbench.store import WorkbenchStore, utc_now


class RunService:
    def __init__(self, store: WorkbenchStore):
        self.store = store
        self.snapshots = SnapshotBuilder(store)

    def cancel(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        self.store.request_cancel(run.job_id)
        return self.store.get_run(run_id)

    def rerun(self, run_id: str, idempotency_key: str, source: str) -> RunRecord:
        """Exact replay references original immutable inputs, never the workspace."""
        if not idempotency_key or source not in ("web", "agent"):
            raise ValueError("invalid submission identity")
        fingerprint = digest(json_bytes({"operation": "rerun", "source": source, "parent_run_id": run_id}))
        with self.store.transaction() as db:
            existing = self._existing(db, idempotency_key, fingerprint)
            if existing is not None:
                return existing[0]
            original = self.store.get_run(run_id, db)
        self.snapshots.verify(FrozenRun(run_id, original.manifest_path, original.manifest_hash), exact=True)
        with self.store.transaction() as db:
            existing = self._existing(db, idempotency_key, fingerprint)
            if existing is not None:
                return existing[0]
            identity, job_id = str(uuid4()), str(uuid4())
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (identity, job_id, original.experiment_id, original.experiment_revision,
                str(original.manifest_path), original.manifest_hash, source, run_id, None))
            next_seq = db.execute("SELECT COALESCE(MAX(enqueue_seq),0)+1 FROM jobs").fetchone()[0]
            db.execute("INSERT INTO jobs (id,kind,state,run_id,enqueue_seq,artifact_root,created_at) VALUES (?,'backtest','queued',?,?,?,?)", (job_id, identity, next_seq, str(self.store.artifact_root), utc_now()))
            db.execute("INSERT INTO idempotency VALUES (?,?,?)", (idempotency_key, fingerprint, json_bytes([identity]).decode()))
            return self.store.get_run(identity, db)

    def _existing(self, db, key: str, fingerprint: str) -> list[RunRecord] | None:
        row = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ValueError("idempotency key conflict")
        return [self.store.get_run(identity, db) for identity in json.loads(row["run_ids_json"])]

    def submit(self, experiment_id: str, expected_revision: int, idempotency_key: str, source: str, parent_run_id: str | None = None) -> RunRecord:
        return self._submit([BatchItem(experiment_id, expected_revision)], idempotency_key, source, "submit", parent_run_id)[0]

    def submit_batch(self, items: list[BatchItem], idempotency_key: str, source: str) -> list[RunRecord]:
        return self._submit(items, idempotency_key, source, "submit_batch", None)

    def _submit(self, items: list[BatchItem], key: str, source: str, operation: str, parent_run_id: str | None) -> list[RunRecord]:
        if not key or source not in ("web", "agent"):
            raise ValueError("invalid submission identity")
        if not items or len(items) > 100:
            raise ValueError("batch size must be between 1 and 100")
        fingerprint = digest(json_bytes({"operation": operation, "source": source, "items": [[item.experiment_id, item.expected_revision] for item in items], "parent_run_id": parent_run_id}))
        with self.store.transaction() as db:
            existing = self._existing(db, key, fingerprint)
            if existing is not None:
                return existing
            if parent_run_id is not None:
                self.store.get_run(parent_run_id, db)
            experiments = []
            for item in items:
                draft = self.store.get_experiment(item.experiment_id, db)
                if draft.revision != item.expected_revision:
                    raise ValueError("experiment revision conflict")
                experiments.append(draft)
        frozen = [self.snapshots.freeze(draft, source, parent_run_id) for draft in experiments]
        with self.store.transaction() as db:
            existing = self._existing(db, key, fingerprint)
            if existing is not None:
                return existing
            for draft in experiments:
                if self.store.get_experiment(draft.id, db).revision != draft.revision:
                    raise ValueError("experiment revision conflict")
            group_id = str(uuid4()) if operation == "submit_batch" else None
            next_seq = db.execute("SELECT COALESCE(MAX(enqueue_seq), 0) + 1 FROM jobs").fetchone()[0]
            records = []
            for offset, (draft, snapshot) in enumerate(zip(experiments, frozen, strict=True)):
                job_id = str(uuid4())
                db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (snapshot.run_id, job_id, draft.id, draft.revision, str(snapshot.manifest_path), snapshot.manifest_hash, source, parent_run_id, group_id))
                records.append(RunRecord(snapshot.run_id, job_id, snapshot.manifest_path, snapshot.manifest_hash, source, parent_run_id, group_id, draft.id, draft.revision))
            db.execute("INSERT INTO idempotency VALUES (?, ?, ?)", (key, fingerprint, json_bytes([record.run_id for record in records]).decode()))
            for offset, record in enumerate(records):
                db.execute("INSERT INTO jobs (id, kind, state, run_id, enqueue_seq, artifact_root, created_at) VALUES (?, 'backtest', 'queued', ?, ?, ?, ?)", (record.job_id, record.run_id, next_seq + offset, str(self.store.artifact_root), utc_now()))
            return [self.store.get_run(record.run_id, db) for record in records]
