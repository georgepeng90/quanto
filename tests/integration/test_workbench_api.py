from fastapi.testclient import TestClient
from uuid import uuid4
from datetime import datetime, timezone

from quanto.api.app import create_app
from tests.integration.test_snapshot_submission import _ready


def test_api_rejects_conflicts_and_invalid_parameters(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/runs/missing", headers=headers).status_code == 404
        assert client.post("/api/runs/batch", json={"items": [], "idempotency_key": "x"}, headers=headers).status_code == 422
        assert client.get("/api/runs/missing", headers={**headers, "Origin": "http://evil.test"}).status_code == 403
        assert client.get("/api/datasets", headers={**headers, "Origin": "http://127.0.0.1:8765"}).status_code == 200
        assert client.get("/api/session", headers={"Host": "evil.test"}).status_code == 403
        assert client.post("/api/experiments", json={"payload": {}}, headers={"Origin": "null", **headers}).status_code == 403
        assert client.get("/api/missing", headers=headers).json()["code"] == "NOT_FOUND"
        assert client.post("/api/uploads", headers=headers).json()["code"] == "INVALID_ARGUMENT"
        for path, payload in (("/api/runs/batch", {"items": [None], "idempotency_key": "x"}),
                              ("/api/comparisons", {"run_ids": None}),
                              ("/api/comparisons", {"run_ids": [None, None]})):
            response = client.post(path, json=payload, headers=headers)
            assert response.status_code == 422
            assert response.json()["code"] == "INVALID_ARGUMENT"
        corrupt = client.post("/api/uploads", files={"file": ("broken.zip", b"not a ZIP")}, headers=headers)
        assert corrupt.status_code == 422
        assert corrupt.json()["code"] == "INVALID_ARGUMENT"
        upload_id = str(uuid4())
        upload_path = tmp_path / "uploads" / f"{upload_id}.zip"
        upload_path.parent.mkdir(parents=True, exist_ok=True)
        upload_path.write_bytes(b"not a ZIP")
        preflight = client.post("/api/datasets/preflight", json={"upload_id": upload_id}, headers=headers)
        assert preflight.status_code == 422
        assert preflight.json()["code"] == "INVALID_ARGUMENT"


def test_list_resources_and_experiment_revision_conflict(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/datasets", headers=headers).json() == {"items": [], "next_cursor": None}
        created = client.post("/api/experiments", json={"payload": {}}, headers=headers).json()
        response = client.put(f"/api/experiments/{created['id']}", json={"expected_revision": 0, "payload": {}}, headers=headers)
        assert response.status_code == 409


def test_dataset_detail_exposes_registered_instruments_for_browser_selection(tmp_path):
    store, _, dataset, _ = _ready(tmp_path)
    app = create_app(store.root)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        detail = client.get(f"/api/datasets/{dataset.id}", headers=headers).json()
        assert detail["instruments"] == [{"symbol": "000001", "lot_size": 100,
                                          "settlement_sessions": 1, "listing_date": "2024-01-01",
                                          "group": "industry"}]
        assert "instruments" not in client.get("/api/datasets", headers=headers).json()["items"][0]


def test_run_and_job_api_expose_persisted_lifecycle_times(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    app = create_app(store.root)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        run = client.post("/api/runs", json={"experiment_id": draft.id, "expected_revision": draft.revision,
                                              "idempotency_key": "times"}, headers=headers).json()
        assert datetime.fromisoformat(run["created_at"]).tzinfo == timezone.utc
        if run["started_at"] is not None:
            assert datetime.fromisoformat(run["started_at"]) >= datetime.fromisoformat(run["created_at"])
        if run["finished_at"] is not None:
            assert run["started_at"] is not None
        job = client.get(f"/api/jobs/{run['job_id']}", headers=headers).json()
        assert job["created_at"] == run["created_at"]
        assert client.get(f"/api/runs/{run['run_id']}", headers=headers).json()["created_at"] == run["created_at"]
        assert client.get("/api/runs", headers=headers).json()["items"][0]["created_at"] == run["created_at"]


def test_idempotency_conflict(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    app = create_app(store.root)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        first = {"experiment_id": draft.id, "expected_revision": draft.revision, "idempotency_key": "same"}
        assert client.post("/api/runs", json=first, headers=headers).status_code == 200
        assert client.post("/api/runs", json={**first, "source": "agent"}, headers=headers).status_code == 409
        assert client.post("/api/runs", json={**first, "expected_revision": "1"}, headers=headers).status_code == 422
        source = client.get(f"/api/strategies/{draft.payload['strategy_revision_id']}", headers=headers).json()
        assert "strategy.py" in source["files"]
        assert "path" not in source


def test_100_valid_batch_items_retry_and_101_rejection(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    # No lifespan: exercise real freeze/enqueue without executing 100 strategies.
    client = TestClient(create_app(store.root), base_url="http://127.0.0.1:8765")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    item = {"experiment_id": draft.id, "expected_revision": draft.revision}
    body = {"items": [item] * 100, "idempotency_key": "hundred"}
    response = client.post("/api/runs/batch", json=body, headers=headers)
    assert response.status_code == 200
    runs = response.json()["items"]
    assert len({run["run_id"] for run in runs}) == 100
    assert len({run["job_id"] for run in runs}) == 100
    assert len({run["group_id"] for run in runs}) == 1
    assert runs[0]["group_id"] is not None
    assert {run["state"] for run in runs} == {"queued"}
    retry = client.post("/api/runs/batch", json=body, headers=headers)
    assert retry.status_code == 200
    assert [(run["run_id"], run["job_id"]) for run in retry.json()["items"]] == [(run["run_id"], run["job_id"]) for run in runs]
    rejected = client.post("/api/runs/batch", json={"items": [item] * 101, "idempotency_key": "too-many"}, headers=headers)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INVALID_ARGUMENT"
    with store.transaction() as db:
        assert tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("runs", "jobs", "idempotency")) == (100, 100, 1)
