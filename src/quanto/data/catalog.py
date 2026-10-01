from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time

from quanto.data.contracts import DataRequirement, FragmentRef, MarketDataError, SnapshotRef, content_key


def compatible(stored, wanted) -> bool:
    if (stored.kind, stored.frequency, stored.currency) != (wanted.kind, wanted.frequency, wanted.currency):
        return False
    if stored.instruments and wanted.instruments and not set(stored.instruments).intersection(wanted.instruments):
        return False
    if stored.price_basis and wanted.price_basis:
        if stored.price_basis.mode != wanted.price_basis.mode:
            return False
        if wanted.price_basis.basis_id and stored.price_basis.basis_id != wanted.price_basis.basis_id:
            return False
    return stored.start < wanted.end and wanted.start < stored.end


@dataclass(frozen=True)
class LeaseToken:
    series_id: str
    owner: str
    generation: int
    expires_at: float


class MarketCatalog:
    def __init__(self, root: Path, clock=time.time):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "catalog.sqlite"
        self.clock = clock
        if any(path.is_symlink() for path in (self.path, *self.path.parents)):
            raise MarketDataError("INTEGRITY_ERROR", "catalog symbolic link")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS series(id TEXT PRIMARY KEY, definition TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fragments(id TEXT PRIMARY KEY, series_id TEXT NOT NULL, payload TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS source_evidence(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, requirement TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0, snapshot_id TEXT);
                CREATE TABLE IF NOT EXISTS leases(series_id TEXT PRIMARY KEY, owner TEXT, generation INTEGER NOT NULL, expires_at REAL NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def register_fragments(self, fragments: tuple[FragmentRef, ...]) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for fragment in fragments:
                    payload = fragment.model_dump_json()
                    db.execute("INSERT OR IGNORE INTO series VALUES (?, ?)", (fragment.series_id, fragment.request.model_dump_json()))
                    db.execute("INSERT OR IGNORE INTO source_evidence VALUES (?, ?)", (fragment.source_evidence_hash, fragment.source.model_dump_json()))
                    db.execute("INSERT OR IGNORE INTO fragments(id, series_id, payload) VALUES (?, ?, ?)", (content_key(fragment.model_dump(mode="json")), fragment.series_id, payload))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def fragments(self, requirement: DataRequirement) -> tuple[FragmentRef, ...]:
        with self.connection() as db:
            rows = db.execute("SELECT payload FROM fragments WHERE active=1 ORDER BY rowid").fetchall()
        fragments = (FragmentRef.model_validate_json(row[0]) for row in rows)
        return tuple(fragment for fragment in fragments if any(compatible(fragment.request, wanted) for wanted in requirement.requests))

    def resolve_snapshot(self, snapshot_id: str) -> SnapshotRef:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise MarketDataError("DATA_NOT_PREPARED", "snapshot is not committed ready")
        return SnapshotRef.model_validate_json(row[0])

    def create_request(self, request_id: str, requirement: DataRequirement) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO requests(id, requirement) VALUES (?, ?)", (request_id, requirement.model_dump_json()))

    def cancel_request(self, request_id: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE requests SET cancelled=1 WHERE id=? AND snapshot_id IS NULL", (request_id,))

    def request_snapshot(self, request_id: str) -> SnapshotRef | None:
        with self.connection() as db:
            row = db.execute("SELECT snapshot_id FROM requests WHERE id=?", (request_id,)).fetchone()
        return self.resolve_snapshot(row[0]) if row and row[0] else None

    def acquire_lease(self, series_id: str, owner: str, ttl: float) -> LeaseToken | None:
        now = self.clock()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM leases WHERE series_id=?", (series_id,)).fetchone()
            if row and row["owner"] is not None and row["expires_at"] > now:
                db.rollback()
                return None
            generation = row["generation"] + 1 if row else 1
            token = LeaseToken(series_id, owner, generation, now + ttl)
            db.execute("INSERT INTO leases VALUES (?, ?, ?, ?) ON CONFLICT(series_id) DO UPDATE SET owner=excluded.owner, generation=excluded.generation, expires_at=excluded.expires_at", (series_id, owner, generation, token.expires_at))
            db.commit()
        return token

    def renew_lease(self, token: LeaseToken, ttl: float) -> LeaseToken:
        expires = self.clock() + ttl
        with self.connection() as db:
            count = db.execute("UPDATE leases SET expires_at=? WHERE series_id=? AND owner=? AND generation=? AND expires_at>?",
                (expires, token.series_id, token.owner, token.generation, self.clock())).rowcount
        if count != 1:
            raise MarketDataError("FAILED", "lease ownership lost")
        return LeaseToken(token.series_id, token.owner, token.generation, expires)

    def release_lease(self, token: LeaseToken) -> None:
        with self.connection() as db:
            db.execute("UPDATE leases SET owner=NULL, expires_at=0 WHERE series_id=? AND owner=? AND generation=?", (token.series_id, token.owner, token.generation))

    def publish_fragments(self, previous: tuple[FragmentRef, ...], new: tuple[FragmentRef, ...], leases: tuple[LeaseToken, ...], request_ids: tuple[str, ...]) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_leases(db, leases)
            if not any(db.execute("SELECT 1 FROM requests WHERE id=? AND cancelled=0", (identity,)).fetchone() for identity in request_ids):
                raise MarketDataError("CANCELLED", "no active request for fragment publication")
            for fragment in new:
                db.execute("INSERT OR IGNORE INTO series VALUES (?, ?)", (fragment.series_id, fragment.request.model_dump_json()))
                db.execute("INSERT OR IGNORE INTO source_evidence VALUES (?, ?)", (fragment.source_evidence_hash, fragment.source.model_dump_json()))
                db.execute("INSERT OR IGNORE INTO fragments(id, series_id, payload) VALUES (?, ?, ?)",
                    (content_key(fragment.model_dump(mode="json")), fragment.series_id, fragment.model_dump_json()))
            new_ids = {content_key(item.model_dump(mode="json")) for item in new}
            for old in previous:
                identity = content_key(old.model_dump(mode="json"))
                if identity not in new_ids:
                    db.execute("UPDATE fragments SET active=0 WHERE id=?", (identity,))
            for identity in new_ids:
                db.execute("UPDATE fragments SET active=1 WHERE id=?", (identity,))
            db.commit()

    def _check_leases(self, db, leases):
        for token in leases:
            row = db.execute("SELECT owner, generation, expires_at FROM leases WHERE series_id=?", (token.series_id,)).fetchone()
            if not row or (row[0], row[1]) != (token.owner, token.generation) or row[2] <= self.clock():
                raise MarketDataError("FAILED", "stale lease cannot publish")

    def commit_ready(self, request_ids: tuple[str, ...], ref: SnapshotRef, leases: tuple[LeaseToken, ...]) -> tuple[str, ...]:
        from quanto.data.storage import MarketStore
        manifest = MarketStore(self.root).verify_snapshot(ref)
        if not manifest.coverage.complete:
            raise MarketDataError("INCOMPLETE", "cannot publish incomplete manifest", manifest.coverage)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._check_leases(db, leases)
            eligible = []
            for identity in request_ids:
                row = db.execute("SELECT requirement, cancelled, snapshot_id FROM requests WHERE id=?", (identity,)).fetchone()
                if row and not row[1] and row[2] is None:
                    if DataRequirement.model_validate_json(row[0]).key != manifest.requirement.key:
                        raise MarketDataError("DATA_NOT_PREPARED", "snapshot does not fulfill registered request")
                    eligible.append(identity)
            if eligible:
                db.execute("INSERT OR IGNORE INTO snapshots VALUES (?, ?)", (ref.id, ref.model_dump_json()))
                for identity in eligible:
                    db.execute("UPDATE requests SET snapshot_id=? WHERE id=?", (ref.id, identity))
            db.commit()
        return tuple(eligible)
