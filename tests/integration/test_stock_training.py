import json
import time

from fastapi.testclient import TestClient
import pytest

from quanto.api.app import create_app
from quanto.backtest.stock_trading import STOCK_TRADING_DEFINITION
from quanto.investing.stock_definitions import STOCK_PPO_DEFINITION
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from tests.fixtures.stock_training_cases import stock_case, stock_identity_evidence


def stock_workspace(tmp_path, monkeypatch, *, missing_rules=False):
    market = tmp_path / "market.json"
    market.write_text(json.dumps({"root": str(tmp_path / "market")}))
    monkeypatch.setenv("QUANTO_MARKET_CONFIG", str(market))
    source = tmp_path / "stock-data"
    spec, _ = stock_case(source)
    if missing_rules:
        manifest = json.loads((source / "manifest.json").read_text())
        del manifest["auxiliary_tables"]["fee_rules"]
        (source / "manifest.json").write_text(json.dumps(manifest))
    app = create_app(tmp_path / "workspace")
    evidence = stock_identity_evidence(source) if not missing_rules else None
    if missing_rules:
        # Freeze an honest declaration of missing evidence, so the capability check must block it.
        from quanto.backtest.dataset_loaders import load_dataset
        from quanto.backtest.dataset_identity import make_import_evidence
        from tests.fixtures.stock_training_cases import stock_binding
        bundle = load_dataset(source)
        bundle.auxiliary["fee_rules"] = bundle.auxiliary["stock_sources"].iloc[:0]
        binding = stock_binding(bundle)
        evidence = make_import_evidence(source, assets=binding.assets, benchmark=binding.benchmark,
            source_document="missing fee fixture", table_semantics=tuple(s for s in binding.table_semantics if s.table != "fee_rules"))
    store = app.state.service.store
    dataset = store.register_dataset(source, identity_evidence=evidence)
    binding = BindingStore(store).get_asset_binding(dataset.id)
    basket = BasketStore(store).create("Stock research", tuple(a.instrument_id for a in binding.assets), "synthetic", "stock")
    del spec["backtest"]["symbols"]
    request = dict(dataset_id=dataset.id, spec=spec, framework_contract_version="multi-asset-v1",
        basket_revision_ref=basket.ref.model_dump(mode="json"), learning_definition_ref=STOCK_PPO_DEFINITION.ref.model_dump(mode="json"),
        trading_definition_ref=STOCK_TRADING_DEFINITION.ref.model_dump(mode="json"))
    client = TestClient(app, base_url="http://127.0.0.1")
    headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
    return app, client, headers, request


def test_stock_capability_blocks_missing_evidence_without_queueing(tmp_path, monkeypatch):
    app, client, headers, request = stock_workspace(tmp_path, monkeypatch, missing_rules=True)
    report = client.post("/api/capabilities/check", headers=headers, json={"operation": "train", **request}).json()
    assert report["status"] == "blocked"
    assert any(check["code"] == "DATA_REQUIREMENT_UNSATISFIED" for check in report["checks"])
    assert app.state.service.store.list_jobs() == []


def test_stock_model_trains_resumes_and_backtests_registered_versions(tmp_path, monkeypatch):
    app, client, headers, request = stock_workspace(tmp_path, monkeypatch)
    from quanto.screening.contracts import ScreeningRequest
    from quanto.screening.store import ScreeningStore
    from tests.fixtures.screening_cases import screening_request
    screenings = ScreeningStore(app.state.service.store)
    selection = screenings.submit(ScreeningRequest.model_validate(screening_request()))
    basket = screenings.create_basket(selection.id, "Screened stocks", "stock-screening")
    request["basket_revision_ref"] = basket.ref.model_dump(mode="json")
    report = client.post("/api/capabilities/check", headers=headers, json={"operation": "train", **request})
    assert report.json()["status"] == "ready", report.text
    created = client.post("/api/investing/models", headers=headers, json=request)
    assert created.status_code == 200, created.text
    model = created.json()
    assert app.state.service.store.list_jobs() == []
    def wait_job(response):
        assert response.status_code == 200, response.text
        job = response.json()
        deadline = time.monotonic() + 90
        while job["state"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.05)
            job = client.get(f'/api/jobs/{job["id"]}', headers=headers).json()
        if job["state"] != "succeeded":
            record = app.state.service.store.get_job(job["id"])
            trace = record.artifact_root / job["id"] / "control" / "traceback.txt"
            pytest.fail(str(job) + (trace.read_text(encoding="utf-8") if trace.exists() else ""))
        return job["id"]
    with client:
        first = wait_job(client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                                     json={"additional_timesteps": 4, "device": "cpu"}))
        second = wait_job(client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                                      json={"additional_timesteps": 4, "device": "cpu"}))
        detail = client.get(f'/api/investing/models/{model["id"]}', headers=headers).json()
        assert detail["total_timesteps"] == 8 and detail["version_count"] == 2
        assert detail["latest_version_id"] == second
        summaries = {}
        for baseline, multiplier in (("policy", 1), ("cash", 1), ("buy_hold", 1), ("buy_hold", 2)):
            run = wait_job(client.post(f'/api/investing/models/{model["id"]}/backtests', headers=headers, json={
                "dataset_id": request["dataset_id"], "version_id": first,
                "spec": dict(start=request["spec"]["validation_start"], end=request["spec"]["validation_end"],
                    baseline=baseline, cost_multiplier=multiplier, evaluation_purpose="development_replay")}))
            summaries[baseline, multiplier] = client.get(f'/api/investing/backtests/{run}', headers=headers).json()["manifest"]
        assert summaries["cash", 1]["metrics"]["execution_cost"] == 0
        assert summaries["buy_hold", 2]["metrics"]["transfer_fee_paid"] > summaries["buy_hold", 1]["metrics"]["transfer_fee_paid"]
        assert any("dividend tax" in warning for warning in summaries["buy_hold", 1]["assumptions"]["limitations"])
        for summary in summaries.values():
            assert summary["execution_provenance"]["selection_origin"]["screening_id"] == selection.id
            assert "not_historical_selection_validation" in summary["execution_provenance"]["selection_evaluation"]
        with app.state.service.store.transaction() as db:
            row = db.execute("SELECT path FROM learning_artifacts WHERE id=?", (first,)).fetchone()
        from pathlib import Path
        assert json.loads((Path(row["path"]) / "screening.json").read_text(encoding="utf-8"))["id"] == selection.id
        assert STOCK_TRADING_DEFINITION.cost_multiplier == 1
        # Resume cannot accept replacements for the frozen data or member order.
        for override in ({"dataset_id": "other"}, {"symbols": ["SSE:stock:600000"]}, {"spec": request["spec"]}):
            response = client.post(f'/api/investing/models/{model["id"]}/train', headers=headers,
                json={"additional_timesteps": 4, **override})
            assert response.status_code == 422
