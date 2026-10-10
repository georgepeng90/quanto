"""An audited pre-framework PPO artifact remains executable without rewriting its bytes."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.investing.training import train_policy
from quanto.learning.artifacts import read_manifest
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.store import utc_now
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.fixtures.investing_training_cases import investing_case
from tests.integration.test_framework_capabilities import _training_case
from tests.integration.test_model_framework_bindings import _old_model


def _wait_job(client, headers, response, store):
    assert response.status_code == 200, response.text
    job = response.json()
    deadline = time.monotonic() + 90
    while job["state"] in {"queued", "running", "cancel_requested"} and time.monotonic() < deadline:
        time.sleep(.05)
        job = client.get(f'/api/jobs/{job["id"]}', headers=headers).json()
    if job["state"] != "succeeded":
        record = store.get_job(job["id"])
        trace = record.artifact_root / job["id"] / "control" / "traceback.txt"
        pytest.fail(f'{job}\n{trace.read_text(encoding="utf-8") if trace.exists() else "no traceback"}')
    return job["id"]


def test_audited_legacy_checkpoint_continues_and_backtests_on_later_coverage(tmp_path):
    pytest.importorskip("stable_baselines3")
    store, create, _ = _training_case(tmp_path)
    original_spec = _old_model(store, create)
    original_dataset = store.get_dataset(create.dataset_id)

    # Make a genuine old-format PPO artifact with neither framework identity nor a fake zip.
    artifact = tmp_path / "old-policy"
    manifest = train_policy(original_spec, original_dataset.path, artifact, lambda _: None, lambda: False,
                            original_dataset.content_hash, model_id="old-model", evaluate_test=False)
    assert manifest.get("framework_learning") is None
    from stable_baselines3 import PPO
    assert PPO.load(artifact / "last.zip", device="cpu").num_timesteps == original_spec["total_timesteps"]
    assert PPO.load(artifact / "policy.zip", device="cpu").action_space.nvec[0] == 3
    files = hash_files(artifact)
    with store.transaction() as db:
        db.execute("""INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,created_at)
                      VALUES (?,'train_policy','succeeded',1,?,?)""", ("old-version", str(tmp_path), utc_now()))
        db.execute("INSERT INTO investing_sessions VALUES (?,?,?)", ("old-version", "old-model", None))
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("old-version", "policy", str(artifact), digest(json_bytes(files)),
                    json_bytes(manifest).decode(), json_bytes(files).decode(), utc_now()))
        old_spec_bytes = db.execute("SELECT spec_json FROM investing_models WHERE id='old-model'").fetchone()[0]
    original_hashes = {path.name: sha256(path.read_bytes()).hexdigest() for path in artifact.iterdir() if path.is_file()}
    linked = BindingStore(store).bind_model("old-model", "audit-real-policy")
    assert linked.origin == "legacy_audited"
    assert linked.evidence["policy_artifacts"]["old-version"]["manifest_id"] == manifest["id"]

    # Later evidence adds another ETF, but keeps the selected canonical and local key unchanged.
    source = tmp_path / "later-source"
    _, dates = investing_case(source)
    first = dates[80]
    for name in ("prices", "status", "benchmark", "calendar"):
        path = source / f"{name}.csv"
        frame = pd.read_csv(path, dtype={"symbol": str})
        frame = frame.loc[pd.to_datetime(frame["date"]) >= first]
        if name in ("prices", "status"):
            frame = pd.concat([frame, frame.assign(symbol="000002")], ignore_index=True)
        frame.to_csv(path, index=False)
    instruments = source / "instruments.csv"
    frame = pd.read_csv(instruments, dtype={"symbol": str})
    pd.concat([frame, frame.assign(symbol="000002")], ignore_index=True).to_csv(instruments, index=False)
    path = source / "manifest.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    values.update(calendar_start=str(first.date()), data_start=str(first.date()))
    path.write_text(json.dumps(values), encoding="utf-8")
    later = store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    later_binding = BindingStore(store).get_asset_binding(later.id)
    assert later.id != original_dataset.id and len(later_binding.assets) == 2
    assert [(pair.instrument_id, pair.local_symbol) for pair in later_binding.resolve(linked.basket.members)] == [
        (pair.instrument_id, pair.local_symbol) for pair in linked.ordered_pairs]
    assert first.date() > create.spec.train_end

    app = create_app(store.root)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
        continued = _wait_job(client, headers, client.post('/api/investing/models/old-model/train', headers=headers,
                                   json={"additional_timesteps": 8, "device": "cpu"}), store)
        detail = client.get('/api/investing/models/old-model', headers=headers).json()
        assert detail["latest_version_id"] == continued
        assert detail["sessions"][0]["base_version_id"] == "old-version"
        with store.transaction() as db:
            continued_root = Path(db.execute("SELECT path FROM learning_artifacts WHERE id=?", (continued,)).fetchone()[0])
        continued_manifest = read_manifest(continued_root, "policy")
        assert continued_manifest["parent_content_id"] == manifest["id"]
        assert continued_manifest["framework"]["operation"] == "resume"
        assert continued_manifest["framework_learning"] is not None

        backtest = {"dataset_id": later.id, "version_id": "old-version",
                    "spec": {"start": str(dates[90].date()), "end": str(dates[99].date()),
                             "evaluation_purpose": "post_selection_backtest", "baseline": "policy"}}
        run = _wait_job(client, headers, client.post('/api/investing/models/old-model/backtests',
                                                     headers=headers, json=backtest), store)
        result = client.get(f'/api/investing/backtests/{run}', headers=headers).json()["manifest"]
        assert result["policy_id"] == "old-version"
        assert result["evaluation_purpose"] == "post_selection_backtest"
        assert result["dataset_id"] == later.id
        assert result["framework"]["asset_binding"]["dataset_version_id"] == later.id
        assert result["framework"]["request"]["dataset_id"] == later.id
        assert result["execution_provenance"]["policy_version_id"] == "old-version"
        assert result["execution_provenance"]["source_hashes"]
        assert [(pair["instrument_id"], pair["local_symbol"]) for pair in result["framework"]["ordered_pairs"]] == [
            (pair.instrument_id.model_dump(mode="json"), pair.local_symbol) for pair in linked.ordered_pairs]
        assert len(result["framework"]["ordered_pairs"]) == 1
        assert result["metadata"]["backtest"]["symbols"] == original_spec["backtest"]["symbols"]
        assert result["counts"]["equity"] > 0

    assert original_hashes == {path.name: sha256(path.read_bytes()).hexdigest()
                               for path in artifact.iterdir() if path.is_file()}
    with store.transaction() as db:
        assert db.execute("SELECT spec_json FROM investing_models WHERE id='old-model'").fetchone()[0] == old_spec_bytes
