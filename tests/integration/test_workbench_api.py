from fastapi.testclient import TestClient

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


def test_list_resources_and_experiment_revision_conflict(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/datasets", headers=headers).json() == {"items": [], "next_cursor": None}
        created = client.post("/api/experiments", json={"payload": {}}, headers=headers).json()
        response = client.put(f"/api/experiments/{created['id']}", json={"expected_revision": 0, "payload": {}}, headers=headers)
        assert response.status_code == 409


def test_idempotency_conflict_and_100_item_boundary(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    app = create_app(store.root)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        first = {"experiment_id": draft.id, "expected_revision": draft.revision, "idempotency_key": "same"}
        assert client.post("/api/runs", json=first, headers=headers).status_code == 200
        assert client.post("/api/runs", json={**first, "source": "agent"}, headers=headers).status_code == 409
        bad = {"items": [{"experiment_id": draft.id, "expected_revision": 1}] * 101, "idempotency_key": "batch"}
        assert client.post("/api/runs/batch", json=bad, headers=headers).status_code == 422
        accepted_size = {"items": [{"experiment_id": "missing", "expected_revision": 1}] * 100, "idempotency_key": "batch"}
        assert client.post("/api/runs/batch", json=accepted_size, headers=headers).status_code == 404
        assert client.post("/api/runs", json={**first, "expected_revision": "1"}, headers=headers).status_code == 422
        source = client.get(f"/api/strategies/{draft.payload['strategy_revision_id']}", headers=headers).json()
        assert "strategy.py" in source["files"]
        assert "path" not in source
