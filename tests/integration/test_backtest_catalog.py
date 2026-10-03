from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.workbench.store import WorkbenchStore


SPEC = {
    "start": "2024-01-01", "end": "2024-01-31", "symbols": ["510300"],
    "initial_cash": 100000, "seed": 7,
    "execution": {"commission_rate": .0003, "minimum_commission": 5,
                  "slippage_rate": .0005, "max_stale_sessions": 0, "model": "next_open"},
    "risk": {"max_total_weight": 1, "max_symbol_weight": 1, "group_caps": {},
             "stress_scenarios": [], "pause_action": "block_new", "recovery_mode": "never"},
    "metrics": {"annual_sessions": 252, "risk_free_rate": 0,
                "cash_rate": 0, "definition_version": "v1"},
}


def _client(tmp_path):
    store = WorkbenchStore(tmp_path / "workspace")
    client = TestClient(create_app(store.root), base_url="http://127.0.0.1")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    return store, client, headers


def _job(store, identity, kind, sequence, state="queued", run_id=None, input_path=None):
    with store.transaction() as db:
        db.execute("INSERT INTO jobs (id,kind,state,run_id,enqueue_seq,artifact_root,input_path,created_at,finished_at,error) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (identity, kind, state, run_id, sequence, str(store.artifact_root),
                    str(input_path) if input_path else None, "2024-02-01T00:00:00+00:00",
                    "2024-02-01T01:00:00+00:00" if state in {"succeeded", "failed"} else None,
                    "worker failed" if state == "failed" else None))


def _fixed(store, identity="fixed", sequence=1, state="queued", spec=SPEC):
    path = store.root / f"{identity}-manifest.json"
    path.write_text(json.dumps({"dataset_content_hash": "same-data", "dataset_manifest": {"price_mode": "adjusted_research"},
                                "strategy_manifest": {"name": "趋势策略"}, "backtest_spec": spec}), encoding="utf-8")
    _job(store, f"{identity}-job", "backtest", sequence, state, identity)
    with store.transaction() as db:
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                   (identity, f"{identity}-job", "experiment", 1, str(path),
                    hashlib.sha256(path.read_bytes()).hexdigest(), "web", None, "batch-1"))


def _policy(store, identity, sequence, state="queued", spec=None, baseline="policy", cost_multiplier=1):
    spec = spec or SPEC
    with store.transaction() as db:
        db.execute("INSERT OR IGNORE INTO datasets VALUES (?,?,?,?)",
                   ("dataset", str(store.root), "same-data", json.dumps({"price_mode": "adjusted_research"})))
        db.execute("INSERT OR IGNORE INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("trained-policy", "policy", str(store.root), "policy-hash",
                    json.dumps({"config": {"name": "PPO 策略", "backtest": spec}}), "{}", "2024-01-01"))
    payload = {"input": {"request": {"dataset_id": "dataset", "policy_id": "trained-policy",
                                      "spec": {"start": spec["start"], "end": spec["end"],
                                               "baseline": baseline, "cost_multiplier": cost_multiplier}},
                         "dataset_ref": {"content_hash": "same-data"},
                         "policy_ref": {"id": "trained-policy", "content_hash": "policy-hash"}}}
    path = store.root / f"{identity}-input.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _job(store, identity, "policy_backtest", sequence, state, input_path=path)
    with store.transaction() as db:
        db.execute("INSERT INTO learning_submissions VALUES (?,?)",
                   (identity, hashlib.sha256(path.read_bytes()).hexdigest()))


def test_catalog_auth_union_states_and_append_stable_pages(tmp_path):
    store, client, headers = _client(tmp_path)
    _fixed(store)
    _job(store, "training", "train_policy", 2)
    _policy(store, "policy", 3, "failed")
    _job(store, "model", "model_backtest", 4)
    assert client.get("/api/backtests/catalog").status_code == 401
    first = client.get("/api/backtests/catalog?limit=2", headers=headers)
    assert first.status_code == 200, first.text
    page = first.json()
    assert [(item["id"], item["kind"], item["state"]) for item in page["items"]] == [
        ("fixed", "fixed", "queued"), ("policy", "policy", "failed")]
    assert page["items"][0]["group_id"] == "batch-1"
    assert page["items"][1]["error"] == "worker failed"
    _job(store, "later", "model_backtest", 5)
    second = client.get("/api/backtests/catalog", params={"cursor": page["next_cursor"], "limit": 2}, headers=headers).json()
    assert [item["id"] for item in second["items"]] == ["model", "later"]
    assert second["next_cursor"] is None
    assert client.get("/api/backtests/catalog?limit=0", headers=headers).status_code == 422
    assert client.get("/api/backtests/catalog?cursor=bad", headers=headers).status_code == 422


def test_fixed_policy_and_baseline_share_exact_condition_key(tmp_path):
    store, client, headers = _client(tmp_path)
    _fixed(store)
    _policy(store, "ppo", 2)
    _policy(store, "equal", 3, baseline="equal_weight")
    later = {**SPEC, "end": "2024-02-01"}
    _policy(store, "later", 4, spec=later)
    _policy(store, "costly", 5, cost_multiplier=2)
    rows = {item["id"]: item for item in client.get("/api/backtests/catalog", headers=headers).json()["items"]}
    assert rows["fixed"]["comparison_key"]
    assert rows["fixed"]["comparison_key"] == rows["ppo"]["comparison_key"] == rows["equal"]["comparison_key"]
    assert rows["later"]["comparison_key"] != rows["ppo"]["comparison_key"]
    assert rows["costly"]["comparison_key"] != rows["ppo"]["comparison_key"]
    assert rows["ppo"]["conditions"]["period"] == {"start": SPEC["start"], "end": SPEC["end"]}
    assert rows["costly"]["conditions"]["execution"]["commission_rate"] == .0006
    assert rows["ppo"]["detail_url"] == "/investing?backtest=ppo"
    assert rows["fixed"]["detail_url"] == "/runs/fixed"


def test_succeeded_row_reports_missing_or_corrupt_saved_result_without_hiding_neighbors(tmp_path):
    store, client, headers = _client(tmp_path)
    _fixed(store, state="succeeded")
    _policy(store, "corrupt", 2, state="succeeded")
    output = store.root / "corrupt-result"
    output.mkdir()
    (output / "summary.json").write_text(json.dumps({"metrics": {"total_return": .2}}), encoding="utf-8")
    with store.transaction() as db:
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("corrupt", "policy_backtest", str(output), "hash", json.dumps({"metrics": {"total_return": .2}}),
                    json.dumps({"summary.json": "wrong-hash"}), "2024-02-01"))
    response = client.get("/api/backtests/catalog", headers=headers)
    assert response.status_code == 200, response.text
    rows = {item["id"]: item for item in response.json()["items"]}
    assert set(rows) == {"fixed", "corrupt"}
    assert all(row["state"] == "succeeded" and row["metrics"] is None and row["result_error"] for row in rows.values())


def test_incomplete_conditions_have_no_comparison_key(tmp_path):
    store, client, headers = _client(tmp_path)
    _fixed(store, spec={**SPEC, "metrics": {"annual_sessions": 252}})
    item = client.get("/api/backtests/catalog", headers=headers).json()["items"][0]
    assert item["comparison_key"] is None


def test_optional_risk_nulls_are_comparable_but_different_baskets_are_not(tmp_path):
    store, client, headers = _client(tmp_path)
    from quanto.config import BacktestSpec
    complete = BacktestSpec.model_validate(SPEC).model_dump(mode="json")
    _fixed(store, "base", 1, spec=complete)
    _fixed(store, "different-basket", 2, spec={**complete, "symbols": ["159915"]})
    rows = {item["id"]: item for item in client.get("/api/backtests/catalog", headers=headers).json()["items"]}
    assert rows["base"]["comparison_key"]
    assert rows["different-basket"]["comparison_key"] != rows["base"]["comparison_key"]


def test_successful_rows_use_validated_saved_metrics_and_policy_backtest_settings(tmp_path):
    store, client, headers = _client(tmp_path)
    _fixed(store, state="succeeded")
    final = store.artifact_root / "fixed-job" / "final"
    final.mkdir(parents=True)
    (final / "metrics.json").write_text(json.dumps({"metrics": {"total_return": .1}}), encoding="utf-8")
    (final / "metadata.json").write_text("{}", encoding="utf-8")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in final.iterdir()}
    control = final.parent / "control"
    control.mkdir()
    (control / "receipt.json").write_text(json.dumps({"result": {"hashes": hashes}}), encoding="utf-8")

    _policy(store, "ppo", 2, state="succeeded")
    output = store.root / "ppo-result"
    output.mkdir()
    saved_spec = {**SPEC, "execution": {**SPEC["execution"], "commission_rate": .0009}}
    summary = {"metrics": {"total_return": .2, "sharpe": None},
               "period": {"start": SPEC["start"], "end": SPEC["end"]},
               "assumptions": {"price_mode": "adjusted_research"},
               "dataset_content_hash": "same-data", "baseline": "policy",
               "metadata": {"config": {"name": "PPO 策略"}, "backtest": saved_spec}}
    (output / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (output / "results.sqlite3").write_bytes(b"saved index")
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in output.iterdir()}
    with store.transaction() as db:
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("ppo", "policy_backtest", str(output), "hash", json.dumps(summary),
                    json.dumps(files), "2024-02-01"))
    rows = {item["id"]: item for item in client.get("/api/backtests/catalog", headers=headers).json()["items"]}
    assert rows["fixed"]["metrics"] == {"total_return": .1}
    assert rows["ppo"]["metrics"] == {"total_return": .2, "sharpe": None}
    assert rows["ppo"]["result_error"] is None
    assert rows["ppo"]["conditions"]["execution"]["commission_rate"] == .0009
    assert rows["fixed"]["comparison_key"] != rows["ppo"]["comparison_key"]


def test_model_backtest_reads_frozen_interval_and_prepared_basket(tmp_path):
    store, client, headers = _client(tmp_path)
    with store.transaction() as db:
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("prepared", "dataset", str(store.root), "prepared-hash",
                    json.dumps({"frequency_minutes": 5, "instruments": ["SH:etf:510300"], "query": {}}),
                    "{}", "2024-01-01"))
    path = store.root / "model-input.json"
    payload = {"input": {"request": {"dataset_id": "prepared", "spec": {
        "start": "2024-01-02T01:30:00+00:00", "end": "2024-01-02T07:00:00+00:00",
        "baseline": "model", "initial_cash": 100000}},
        "dataset_ref": {"content_hash": "prepared-hash"}}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    _job(store, "model", "model_backtest", 1, input_path=path)
    with store.transaction() as db:
        db.execute("INSERT INTO learning_submissions VALUES (?,?)",
                   ("model", hashlib.sha256(path.read_bytes()).hexdigest()))
    item = client.get("/api/backtests/catalog", headers=headers).json()["items"][0]
    assert item["kind"] == "model"
    assert item["start"] == payload["input"]["request"]["spec"]["start"]
    assert item["symbols"] == ["SH:etf:510300"]
    assert item["conditions"]["valuation_frequency"] == "5m"
    assert item["detail_url"] == "/learning?backtest=model"
