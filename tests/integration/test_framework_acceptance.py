"""Release-boundary checks use isolated copies, never the research workspace."""
import hashlib
import json
import shutil
import sqlite3

from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.workbench.store import WorkbenchStore
from tests.integration.test_framework_storage import dump, legacy_workspace
from tests.integration.test_snapshot_submission import _ready


def test_migration_rehearsal_on_copy_preserves_source_files_and_historical_rows(tmp_path, record_property):
    original = legacy_workspace(tmp_path / "original")
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in original.iterdir()}
    copied = tmp_path / "rehearsal"
    shutil.copytree(original, copied)
    store = WorkbenchStore(copied)
    with store.transaction() as db:
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("datasets", "experiments", "jobs", "investing_models", "baskets", "model_framework_bindings")}
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        from quanto.workbench.migrations import SCHEMA_VERSION
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert counts == {"datasets": 1, "experiments": 1, "jobs": 1, "investing_models": 1, "baskets": 0, "model_framework_bindings": 0}
    upgraded = dump(store.db_path)
    WorkbenchStore(copied)
    assert dump(store.db_path) == upgraded
    assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in original.iterdir()} == before
    for name in ("historical.parquet", "historical-policy.zip"):
        assert hashlib.sha256((copied / name).read_bytes()).hexdigest() == before[name]
    with sqlite3.connect(original / "workbench.sqlite3") as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    record_property("migration_evidence", json.dumps({"source_hashes": before, "post_upgrade_rows": counts,
                                                     "repeat_upgrade_unchanged": True}))


def test_stock_selection_with_etf_execution_and_data_is_blocked(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    app = create_app(store.root)
    # No lifespan worker is needed: checks and draft saves are read/metadata operations.
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    response = client.post("/api/baskets", headers=headers, json={
        "name": "Companies to research", "selection_note": "Selection only; stock data not supplied",
        "members": [{"exchange": "SZSE", "instrument_type": "stock", "code": "000001"}],
        "idempotency_key": "stock-selection"})
    assert response.status_code == 200, response.text
    basket = response.json()
    assert basket["members"][0]["instrument_type"] == "stock"
    payload = {**draft.payload, "basket_revision_ref": {"basket_id": basket["basket_id"], "revision": basket["revision"]}}
    saved = client.post("/api/experiments", headers=headers, json={"payload": payload})
    assert saved.status_code == 200, saved.text
    checked = client.post("/api/capabilities/check", headers=headers,
                          json={"operation": "fixed_backtest", **payload})
    assert checked.status_code == 200 and checked.json()["status"] == "blocked"
    blocked = client.post("/api/runs", headers=headers, json={"experiment_id": saved.json()["id"],
        "expected_revision": saved.json()["revision"], "idempotency_key": "unsupported-stock-run"})
    assert blocked.status_code == 422 and blocked.json()["code"] == "FRAMEWORK_BLOCKED"
    assert store.list_jobs() == []
