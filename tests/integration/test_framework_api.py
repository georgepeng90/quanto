from fastapi.testclient import TestClient

from quanto.api.app import create_app


MEMBER = {"exchange": "SSE", "instrument_type": "etf", "code": "510300"}


def test_narrow_framework_routes_are_authenticated_and_reachable(tmp_path):
    app = create_app(tmp_path / "workbench")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/definitions").status_code == 401
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        definitions = client.get("/api/definitions", headers=headers)
        assert definitions.status_code == 200
        assert len(definitions.json()["items"]) == 11
        assert {item["ref"]["kind"] for item in definitions.json()["items"]} == {"feature", "learning", "trading"}

        body = {"name": "ETF", "members": [MEMBER], "selection_note": "Research", "idempotency_key": "basket-1"}
        created = client.post("/api/baskets", json=body, headers=headers)
        assert created.status_code == 200, created.text
        revision = created.json()
        identity = revision["basket_id"]
        assert client.post("/api/baskets", json=body, headers=headers).json() == revision
        assert client.get("/api/baskets", headers=headers).json()["items"][0]["basket_id"] == identity
        detail = client.get(f"/api/baskets/{identity}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["current"] == revision
        assert detail.json()["revisions"][0]["content_hash"] == revision["content_hash"]
        assert client.get(f"/api/baskets/{identity}/revisions/1", headers=headers).json() == revision

        revised = client.post(f"/api/baskets/{identity}/revisions", headers=headers,
                              json={"expected_revision": 1, "members": [MEMBER], "selection_note": "Review"})
        assert revised.status_code == 200
        assert revised.json()["revision"] == 2
        stale = client.post(f"/api/baskets/{identity}/revisions", headers=headers,
                            json={"expected_revision": 1, "members": [MEMBER], "selection_note": "Stale"})
        assert stale.status_code == 409


def test_capability_check_accepts_discriminator_and_reports_blocking(tmp_path):
    app = create_app(tmp_path / "workbench")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        body = {"operation": "fetch", "instruments": [MEMBER], "start_date": "2025-01-01",
                "end_date": "2025-01-31"}
        checked = client.post("/api/capabilities/check", json=body, headers=headers)
        assert checked.status_code == 200, checked.text
        assert checked.json()["operation"] == "fetch"
        assert checked.json()["status"] in {"ready", "blocked"}
        assert client.post("/api/capabilities/check", json={**body, "operation": "unknown"},
                           headers=headers).status_code == 422
        fetch = client.post("/api/jobs/data", headers=headers, json={"kind": "fetch", "payload": {
            key: value for key, value in {**body, "auxiliary": {"unknown": {"columns": ["x"]}}}.items()
            if key != "operation"}})
        assert fetch.status_code == 422
        assert fetch.json()["code"] == "FRAMEWORK_BLOCKED"
        assert fetch.json()["details"]["report"]["status"] == "blocked"


def test_retired_write_routes_are_absent(tmp_path):
    app = create_app(tmp_path / "workbench")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        for path in ("/api/learning/datasets", "/api/learning/train", "/api/learning/backtests",
                     "/api/investing/train", "/api/investing/backtests",
                     "/api/datasets/unknown/asset-binding", "/api/models/unknown/bind"):
            response = client.post(path, json={}, headers=headers)
            assert response.status_code in {404, 405}, (path, response.text)


def test_submission_condition_change_is_conflict_with_current_report(tmp_path, monkeypatch):
    from quanto.workbench.snapshots import SnapshotBuilder
    from tests.integration.test_snapshot_submission import _ready

    store, draft, dataset, _ = _ready(tmp_path)
    app = create_app(store.root)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    original = SnapshotBuilder.freeze

    def change_after_freeze(self, *args, **kwargs):
        frozen = original(self, *args, **kwargs)
        with store.transaction() as db:
            db.execute("UPDATE datasets SET content_hash=? WHERE id=?", ("0" * 64, dataset.id))
        return frozen

    monkeypatch.setattr(SnapshotBuilder, "freeze", change_after_freeze)
    body = {"experiment_id": draft.id, "expected_revision": draft.revision, "idempotency_key": "changed"}
    response = client.post("/api/runs", headers=headers, json=body)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "FRAMEWORK_CONFLICT"
    assert response.json()["details"]["report"]["status"] == "blocked"
    assert store.list_jobs() == []
    assert not list((store.root / "snapshots").iterdir())
    assert (store.root / "dataset_objects" / dataset.content_hash).is_dir()
    # The same unavailable condition found at initial preflight is a blocked request.
    response = client.post("/api/runs", headers=headers, json={**body, "idempotency_key": "blocked"})
    assert response.status_code == 422
    assert response.json()["code"] == "FRAMEWORK_BLOCKED"
