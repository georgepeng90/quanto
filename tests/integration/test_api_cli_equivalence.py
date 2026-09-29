from fastapi.testclient import TestClient
import time
import io
import json
import zipfile

from quanto.api.app import create_app
from quanto.cli import WorkbenchClient
from tests.integration.test_worker_snapshot import ready_run


def test_cli_reads_same_run_as_api(tmp_path):
    store, run, strategy = ready_run(tmp_path)
    app = create_app(store.root)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/session").json()["token"]
        command = WorkbenchClient("http://127.0.0.1:8765", token, transport=client)
        deadline = time.monotonic() + 20
        headers = {"Authorization": f"Bearer {token}"}
        while time.monotonic() < deadline:
            record = command.get(f"/runs/{run.run_id}")
            if record["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)
        assert record["state"] == "succeeded", record
        api = client.get(f"/api/runs/{run.run_id}/results", headers=headers).json()
        cli = command.get(f"/runs/{run.run_id}/results")
        assert cli == api
        assert cli["run_id"] == run.run_id
        assert cli["metadata"]["strategy_revision_id"] == strategy.id
        assert cli["metadata"]["dataset_version_id"]
        assert cli["metadata"]["price_mode"] == "raw_cash"
        assert cli["detail_url"] == f"/runs/{run.run_id}"
        provenance = command.get(f"/runs/{run.run_id}")["provenance"]
        assert provenance["backtest_spec"]["seed"] == 7
        assert provenance["strategy_source_hashes"]
        assert provenance["engine_source_hashes"]
        assert provenance["environment_fingerprint"]
        rows = command.get(f"/runs/{run.run_id}/tables/equity")["items"]
        assert isinstance(rows[0]["equity"], float)
        signals = command.get(f"/runs/{run.run_id}/tables/signals")["items"]
        assert isinstance(signals[0]["weights"], dict)
        first_log = command.get(f"/runs/{run.run_id}/logs?limit=1")
        second_log = command.get(f"/runs/{run.run_id}/logs?limit=1&cursor={first_log['next_cursor']}")
        assert first_log["items"][0]["seq"] < second_log["items"][0]["seq"]
        final = store.artifact_root / run.job_id / "final"
        for format in ("json", "csv"):
            response = client.get(f"/api/runs/{run.run_id}/exports/{format}", headers=headers)
            assert response.headers["content-type"] == "application/zip"
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                assert archive.read("metadata.json") == (final / "metadata.json").read_bytes()
                assert archive.read("metrics.json") == (final / "metrics.json").read_bytes()
                assert archive.read("manifest.json") == (final / "manifest.json").read_bytes()
                if format == "csv":
                    assert archive.read("orders.csv") == (final / "orders.csv").read_bytes()
                    assert archive.read("fills.csv") == (final / "fills.csv").read_bytes()
        assert client.get(f"/api/runs/{run.run_id}/exports/html", headers=headers).content == (final / "report.html").read_bytes()
        fresh = client.post("/api/runs", json={"experiment_id": run.experiment_id,
            "expected_revision": run.experiment_revision, "idempotency_key": "fresh"}, headers=headers).json()
        assert fresh["run_id"] != run.run_id
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if command.get(f"/runs/{fresh['run_id']}")["state"] == "succeeded":
                break
            time.sleep(0.05)
        fresh_result = command.get(f"/runs/{fresh['run_id']}/results")
        assert fresh_result["metrics"] == cli["metrics"]
        fresh_job = store.get_run(fresh["run_id"]).job_id
        assert (store.artifact_root / fresh_job / "final" / "manifest.json").read_bytes() == (final / "manifest.json").read_bytes()
        repeated = client.post(f"/api/runs/{run.run_id}/rerun", json={"idempotency_key": "second"}, headers=headers).json()
        assert repeated["run_id"] != run.run_id
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if command.get(f"/runs/{repeated['run_id']}")["state"] == "succeeded":
                break
            time.sleep(0.05)
        comparison = client.post("/api/comparisons", json={"run_ids": [run.run_id, repeated["run_id"]]}, headers=headers).json()
        assert comparison["directly_comparable"]
        assert comparison["differences"] == {}
