"""Framework metadata uses the same atomic workspace transaction as old records."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from quanto.data.contracts import InstrumentId
from quanto.framework import AssetBinding, BasketRevisionRef
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.framework_cases import binding_values, instrument_values


MEMBERS = tuple(InstrumentId(**instrument_values(code)) for code in ("000001", "000002"))


def legacy_workspace(root):
    """Old metadata with a job table that still needs the learning migration."""
    root.mkdir()
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        db.executescript("""
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (1);
            CREATE TABLE datasets (id TEXT PRIMARY KEY, path TEXT NOT NULL, content_hash TEXT NOT NULL, manifest_json TEXT NOT NULL);
            CREATE TABLE experiments (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload_json TEXT NOT NULL);
            INSERT INTO experiments VALUES ('draft', 3, '{"name":"old"}');
            CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('backtest','demo','fetch','import','validate_strategy')), state TEXT NOT NULL, run_id TEXT UNIQUE, enqueue_seq INTEGER NOT NULL UNIQUE, artifact_root TEXT NOT NULL, pid INTEGER, process_start TEXT, heartbeat TEXT, completed_through TEXT, error TEXT);
            INSERT INTO jobs (id,kind,state,run_id,enqueue_seq,artifact_root) VALUES ('job','backtest','succeeded','run',1,'old-artifact');
            CREATE TABLE investing_models (id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, spec_json TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO investing_models VALUES ('model','dataset-1','{"name":"historical"}','2024-01-01T00:00:00Z');
        """)
        db.execute("INSERT INTO datasets VALUES (?,?,?,?)", ("dataset-1", "old-data", "a" * 64, '{}'))
    (root / "historical.parquet").write_bytes(b"immutable dataset fixture")
    (root / "historical-policy.zip").write_bytes(b"immutable policy fixture")
    return root


def dump(path):
    with sqlite3.connect(path) as db:
        return tuple(db.iterdump())


def test_new_and_repeated_initialization(tmp_path):
    root = tmp_path / "workspace"
    store = WorkbenchStore(root)
    with store.transaction() as db:
        assert db.execute("SELECT version FROM schema_version").fetchall()[0][0] == 3
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    before = dump(store.db_path)
    WorkbenchStore(root)
    assert dump(store.db_path) == before


def test_v1_upgrade_keeps_existing_data_and_is_repeatable(tmp_path):
    root = legacy_workspace(tmp_path / "legacy")
    files = {path.name: path.read_bytes() for path in root.iterdir() if path.suffix != ".sqlite3"}
    store = WorkbenchStore(root)
    assert store.get_experiment("draft").revision == 3
    assert store.get_job("job").state == "succeeded"
    assert store.get_dataset("dataset-1").content_hash == "a" * 64
    with store.transaction() as db:
        assert json.loads(db.execute("SELECT spec_json FROM investing_models").fetchone()[0]) == {"name": "historical"}
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    migrated = dump(store.db_path)
    WorkbenchStore(root)
    assert dump(store.db_path) == migrated
    assert {name: (root / name).read_bytes() for name in files} == files


@pytest.mark.parametrize("after_statement", ["DROP TABLE jobs", "CREATE TABLE basket_revisions", "UPDATE schema_version"])
def test_failed_migration_rolls_back_ddl_rows_and_version(tmp_path, monkeypatch, after_statement):
    root = legacy_workspace(tmp_path / "legacy")
    before = dump(root / "workbench.sqlite3")
    files = {path.name: path.read_bytes() for path in root.iterdir() if path.suffix != ".sqlite3"}
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if sql.strip().startswith(after_statement):
                raise RuntimeError("injected migration failure")
            return result

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **kwargs, factory=FailingConnection))
        with pytest.raises(RuntimeError, match="injected"):
            WorkbenchStore(root)
    assert dump(root / "workbench.sqlite3") == before
    assert {name: (root / name).read_bytes() for name in files} == files
    WorkbenchStore(root)


@pytest.mark.parametrize("versions", [[], [1, 1], [4]])
def test_invalid_existing_version_is_rejected_without_changes(tmp_path, versions):
    root = legacy_workspace(tmp_path / "legacy")
    with sqlite3.connect(root / "workbench.sqlite3") as db:
        db.execute("DELETE FROM schema_version")
        db.executemany("INSERT INTO schema_version VALUES (?)", [(version,) for version in versions])
    before = dump(root / "workbench.sqlite3")
    with pytest.raises(ValueError, match="schema version"):
        WorkbenchStore(root)
    assert dump(root / "workbench.sqlite3") == before


def test_basket_revisions_preserve_order_and_old_content(tmp_path):
    store = WorkbenchStore(tmp_path / "workspace")
    baskets = BasketStore(store)
    first = baskets.create("watchlist", MEMBERS, "selected", "create-1")
    second = baskets.revise(first.basket_id, first.revision, MEMBERS[::-1], "reordered")
    assert second.revision == 2
    assert second.members == MEMBERS[::-1]
    assert baskets.get(first.ref) == first
    assert baskets.get(second.ref) == second
    with pytest.raises(ValueError, match="revision conflict"):
        baskets.revise(first.basket_id, first.revision, MEMBERS, "stale")
    with pytest.raises(KeyError):
        baskets.get(BasketRevisionRef(basket_id=first.basket_id, revision=3))
    with store.transaction() as db:
        assert db.execute("SELECT current_revision FROM baskets").fetchone()[0] == 2
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_create_idempotency_includes_payload_and_order(tmp_path):
    store = WorkbenchStore(tmp_path / "workspace")
    baskets = BasketStore(store)
    first = baskets.create("watchlist", MEMBERS, "selected", "same-key")
    assert baskets.create("watchlist", MEMBERS, "selected", "same-key") == first
    for name, members, note in [("other", MEMBERS, "selected"), ("watchlist", MEMBERS[::-1], "selected"), ("watchlist", MEMBERS, "other")]:
        with pytest.raises(ValueError, match="idempotency conflict"):
            baskets.create(name, members, note, "same-key")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM baskets").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM framework_idempotency").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 0


def test_concurrent_creation_and_revision_have_one_winner(tmp_path):
    store = WorkbenchStore(tmp_path / "workspace")
    barrier = Barrier(2)

    def create(_):
        barrier.wait()
        return BasketStore(store).create("watchlist", MEMBERS, "selected", "same-key")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, duplicate = list(pool.map(create, range(2)))
    assert first == duplicate

    def revise(note):
        barrier.wait()
        try:
            return BasketStore(store).revise(first.basket_id, 1, MEMBERS[::-1], note)
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(revise, ["editor-a", "editor-b"]))
    assert sum(isinstance(value, str) and "revision conflict" in value for value in results) == 1
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM basket_revisions").fetchone()[0] == 2
    assert BasketStore(store).get(first.ref) == first


def test_foreign_keys_reject_missing_framework_references(tmp_path):
    store = WorkbenchStore(tmp_path / "workspace")
    statements = [
        ("INSERT INTO basket_revisions VALUES (?,?,?,?,?)", ("missing", 1, "{}", "a" * 64, "now")),
        ("INSERT INTO baskets VALUES (?,?,?,?)", ("orphan-head", "name", 1, "now")),
        ("INSERT INTO dataset_asset_bindings VALUES (?,?,?,?)", ("missing", "a" * 64, "{}", "b" * 64)),
        ("INSERT INTO model_framework_bindings VALUES (?,?,?,?,?,?,?,?,?)", ("missing", "a" * 64, "missing", "b" * 64, "missing", 1, "c" * 64, "{}", "d" * 64)),
    ]
    for sql, values in statements:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            with store.transaction() as db:
                db.execute(sql, values)


def test_binding_first_publication_is_immutable_and_hash_checked(tmp_path):
    store = WorkbenchStore(legacy_workspace(tmp_path / "legacy"))
    bindings = BindingStore(store)
    binding = AssetBinding(**binding_values())
    with pytest.raises(KeyError):
        bindings.get_asset_binding(binding.dataset_version_id)
    with store.transaction() as db:
        bindings._insert_asset_binding(db, binding)
        bindings._insert_asset_binding(db, binding)
    assert bindings.get_asset_binding("dataset-1") == binding
    changed = binding_values()
    changed["assets"][0]["evidence_ref"] = "different-source"
    with pytest.raises(ValueError, match="binding conflict"):
        with store.transaction() as db:
            bindings._insert_asset_binding(db, AssetBinding(**changed))
    changed["dataset_content_hash"] = "c" * 64
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        with store.transaction() as db:
            bindings._insert_asset_binding(db, AssetBinding(**changed))
    assert bindings.get_asset_binding("dataset-1") == binding
    with store.transaction() as db:
        db.execute("UPDATE dataset_asset_bindings SET binding_hash=?", ("0" * 64,))
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        bindings.get_asset_binding("dataset-1")


def test_creation_rolls_back_head_when_revision_insertion_fails(tmp_path):
    store = WorkbenchStore(tmp_path / "workspace")
    with store.transaction() as db:
        db.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON basket_revisions BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        BasketStore(store).create("watchlist", MEMBERS, "selected", "same-key")
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM baskets").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM framework_idempotency").fetchone()[0] == 0
