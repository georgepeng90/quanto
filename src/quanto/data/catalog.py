from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

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


class MarketCatalog:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "catalog.sqlite"
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
