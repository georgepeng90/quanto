"""Catalog and comparison use the submitted framework identity, not local symbols."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.workbench.backtest_catalog import catalog
from quanto.workbench.results import framework_metadata
from quanto.workbench.runs import RunService
from quanto.workbench.service import WorkbenchService
from tests.integration.test_snapshot_submission import _ready
from tests.integration.test_worker_snapshot import ready_run, run_until_finished


def test_catalog_exposes_canonical_identity_from_frozen_run(tmp_path):
    store, draft, dataset, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "catalog-framework", "web")
    app = create_app(store.root)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"Authorization": f"Bearer {client.get('/api/session').json()['token']}"}
        response = client.get("/api/backtests/catalog", headers=headers)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["id"] == run.run_id
    assert item["canonical_assets"] == ["SZSE:etf:000001"]
    assert item["ordered_pairs"][0]["local_symbol"] == "SZSE:etf:000001"
    assert item["asset_types"] == ["etf"]
    assert item["basket_revision_ref"] == draft.payload["basket_revision_ref"]
    assert item["dataset_source"] == dataset.manifest["source"]
    assert item["definition_evidence"]["trading"]["ref"]
    assert item["execution_provenance"]["source_hashes"]
    assert item["comparison_key"]


def test_identical_framework_runs_remain_directly_comparable(tmp_path):
    store, first, _ = ready_run(tmp_path)
    assert run_until_finished(store, first.job_id).state == "succeeded"
    second = RunService(store).submit(first.experiment_id, first.experiment_revision, "second", "web")
    assert run_until_finished(store, second.job_id).state == "succeeded"
    runs = [first, second]
    compared = WorkbenchService(store.root).comparison([run.run_id for run in runs])
    assert compared["directly_comparable"] is True
    assert compared["runs"][0]["comparison_key"] == compared["runs"][1]["comparison_key"]
    assert compared["runs"][0]["canonical_assets"] == ["SZSE:etf:000001"]
    rows = catalog(WorkbenchService(store.root))
    assert all(item["comparison_key"] and item["definition_evidence"] and item["execution_provenance"]
               and item["result_error"] is None for item in rows["items"])
    frozen_input = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    frozen_input["files"]["src/quanto/backtest/engine.py"] = "0" * 64
    first.manifest_path.write_text(json.dumps(frozen_input), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen input changed"):
        WorkbenchService(store.root)._run_provenance(store.get_run(first.run_id))
    service = WorkbenchService(store.root)
    assert service.results(first.run_id)["metadata"]["framework"]["asset_binding"]["binding_hash"]
    with store.transaction() as db:
        viewed = db.execute("SELECT interval_start,interval_end FROM result_views WHERE run_id=? ORDER BY viewed_at DESC LIMIT 1",
                            (first.run_id,)).fetchone()
    assert tuple(viewed) == ("2024-01-01", "2024-01-03")
    with pytest.raises(ValueError, match="frozen input changed"):
        service.comparison([run.run_id for run in runs])
    with pytest.raises(ValueError, match="frozen input changed"):
        catalog(WorkbenchService(store.root))

    draft = store.get_experiment(first.experiment_id)
    changed = store.save_experiment(None, None, {**draft.payload, "sample_purpose": "independent-research"})
    other_input = RunService(store).submit(changed.id, changed.revision, "different-valid-input", "web")
    with store.transaction() as db:
        db.execute("UPDATE runs SET manifest_path=?, manifest_hash=? WHERE run_id=?",
                   (str(other_input.manifest_path), other_input.manifest_hash, first.run_id))
    with pytest.raises(ValueError, match="frozen input reference differs"):
        service.comparison([run.run_id for run in runs])


def test_comparison_key_uses_canonical_identity_and_frozen_economic_context(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "comparison-context", "web")
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    frozen = manifest["resolved_configuration"]
    provenance = WorkbenchService(store.root)._run_provenance(run)["execution_provenance"]
    base = framework_metadata(frozen, provenance)
    assert base["comparison_key"]
    assert base["framework"] == frozen
    assert base["sample_purpose"] == "exploration"
    assert base["evaluation_purpose"] is None

    alternate_id = copy.deepcopy(frozen)
    for member in (alternate_id["basket"]["members"][0], alternate_id["asset_binding"]["assets"][0]["instrument_id"],
                   alternate_id["ordered_pairs"][0]["instrument_id"]):
        member["exchange"] = "SSE"
    alternate_id["basket"]["content_hash"] = ""
    alternate_id["asset_binding"]["binding_hash"] = ""
    assert alternate_id["ordered_pairs"][0]["instrument_id"]["code"] == frozen["ordered_pairs"][0]["instrument_id"]["code"]
    assert framework_metadata(alternate_id, provenance)["comparison_key"] != base["comparison_key"]

    alternate_local = copy.deepcopy(frozen)
    for pair in (alternate_local["asset_binding"]["assets"][0], alternate_local["ordered_pairs"][0]):
        pair["local_symbol"] = "LOCAL-ONE"
    alternate_local["internal_backtest_spec"]["symbols"] = ["LOCAL-ONE"]
    alternate_local["asset_binding"]["binding_hash"] = ""
    assert framework_metadata(alternate_local, provenance)["comparison_key"] == base["comparison_key"]

    alternate_method = copy.deepcopy(frozen)
    alternate_method["request"]["strategy_revision_id"] = "another-strategy"
    alternate_source = copy.deepcopy(provenance)
    strategy_path = next(name for name in alternate_source["source_hashes"] if name.startswith("strategy/"))
    alternate_source["source_hashes"][strategy_path] = "0" * 64
    assert framework_metadata(alternate_method, alternate_source)["comparison_key"] == base["comparison_key"]

    for path, value in (("sample_purpose", "validation"), ("benchmark_basis", "total_return"),
                        ("table_unit", "USD"), ("benchmark_identity", "other-index")):
        changed = copy.deepcopy(frozen)
        if path == "sample_purpose":
            changed["request"]["sample_purpose"] = value
        elif path == "benchmark_basis":
            changed["asset_binding"]["benchmark"]["return_basis"] = value
            changed["asset_binding"]["binding_hash"] = ""
        elif path == "benchmark_identity":
            changed["asset_binding"]["benchmark"].update(instrument_id=None, custom_series_id=value)
            changed["asset_binding"]["binding_hash"] = ""
        else:
            changed["asset_binding"]["table_semantics"][0]["schema_definition"]["unit"] = value
            changed["asset_binding"]["table_semantics"][0]["declaration_hash"] = ""
            changed["asset_binding"]["binding_hash"] = ""
        assert framework_metadata(changed, provenance)["comparison_key"] != base["comparison_key"], path

    changed_source = copy.deepcopy(provenance)
    path = next(name for name in changed_source["source_hashes"] if name.startswith("src/quanto/backtest/"))
    changed_source["source_hashes"][path] = "0" * 64
    assert framework_metadata(frozen, changed_source)["comparison_key"] != base["comparison_key"]
    changed_environment = {**provenance, "environment_fingerprint": "0" * 64}
    assert framework_metadata(frozen, changed_environment)["comparison_key"] != base["comparison_key"]
    incomplete_source = copy.deepcopy(provenance)
    del incomplete_source["source_hashes"]["src/quanto/backtest/engine.py"]
    assert framework_metadata(frozen, incomplete_source)["comparison_key"] is None
    assert framework_metadata(frozen)["comparison_key"] is None


def test_policy_evaluation_purpose_separates_direct_comparison(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "policy-purpose-context", "web")
    frozen = json.loads(run.manifest_path.read_text(encoding="utf-8"))["resolved_configuration"]
    provenance = WorkbenchService(store.root)._run_provenance(run)["execution_provenance"]
    policy = copy.deepcopy(frozen)
    policy["operation"] = policy["capability_report"]["operation"] = "policy_backtest"
    policy["request"] = {"spec": {"evaluation_purpose": "development_replay"}}
    replay = framework_metadata(policy, provenance)
    selected = copy.deepcopy(policy)
    selected["request"]["spec"]["evaluation_purpose"] = "post_selection_backtest"
    assert replay["evaluation_purpose"] == "development_replay"
    assert replay["sample_purpose"] is None
    assert framework_metadata(selected, provenance)["comparison_key"] != replay["comparison_key"]
