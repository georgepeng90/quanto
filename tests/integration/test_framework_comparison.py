"""Catalog and comparison use the submitted framework identity, not local symbols."""
import copy
import json

import pytest
from fastapi.testclient import TestClient

from quanto.api.app import create_app
from quanto.workbench.backtest_catalog import catalog
from quanto.framework import AssetBinding
from quanto.workbench.bindings import BindingStore
from quanto.workbench.run_contexts import ContextStore, create_context
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



def test_comparison_key_uses_verified_identity_and_original_execution_hashes(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "comparison-context", "web")
    original = ContextStore(store).get(run.job_id)
    binding = BindingStore(store).get_asset_binding(original["dataset_version_id"])

    def context(*, asset_binding=binding, spec=None, provenance=None, sample_purpose="exploration"):
        backtest = spec or original["backtest_spec"]
        return create_context(kind="fixed", dataset_id=original["dataset_version_id"],
            asset_binding=asset_binding, ordered_symbols=backtest["symbols"], backtest_spec=backtest,
            execution_provenance=provenance or original["execution_provenance"],
            input_hash=original["input_hash"], basket_revision_ref=original["basket_revision_ref"],
            name=original["name"], method=original["method"],
            dataset_source=original["dataset_source"], sample_purpose=sample_purpose,
            definition_evidence=original["definition_evidence"],
            strategy_revision_id=original["strategy_revision_id"])

    key = original["comparison_key"]
    assert key and context()["comparison_key"] == key
    changed_identity = binding.model_dump(mode="json")
    changed_identity["assets"][0]["instrument_id"]["exchange"] = "SSE"
    changed_identity["binding_hash"] = ""
    assert context(asset_binding=AssetBinding.model_validate(changed_identity))["comparison_key"] != key

    changed_alias = binding.model_dump(mode="json")
    changed_alias["assets"][0]["local_symbol"] = "LOCAL-ONE"
    changed_alias["binding_hash"] = ""
    alias_spec = copy.deepcopy(original["backtest_spec"])
    alias_spec["symbols"] = ["LOCAL-ONE"]
    assert context(asset_binding=AssetBinding.model_validate(changed_alias), spec=alias_spec)["comparison_key"] == key

    assert context(sample_purpose="validation")["comparison_key"] != key
    changed_benchmark = binding.model_dump(mode="json")
    changed_benchmark["benchmark"]["custom_series_id"] = "different-benchmark"
    changed_benchmark["binding_hash"] = ""
    assert context(asset_binding=AssetBinding.model_validate(changed_benchmark))["comparison_key"] != key
    changed_table = binding.model_dump(mode="json")
    changed_table["table_semantics"][0]["schema_definition"]["unit"] = "USD"
    changed_table["table_semantics"][0]["declaration_hash"] = ""
    changed_table["binding_hash"] = ""
    assert context(asset_binding=AssetBinding.model_validate(changed_table))["comparison_key"] != key

    changed_strategy = copy.deepcopy(original["execution_provenance"])
    strategy_path = next(name for name in changed_strategy["source_hashes"] if name.startswith("strategy/"))
    changed_strategy["source_hashes"][strategy_path] = "0" * 64
    assert context(provenance=changed_strategy)["comparison_key"] == key
    changed_engine = copy.deepcopy(original["execution_provenance"])
    engine_path = next(name for name in changed_engine["source_hashes"] if name.startswith("src/quanto/backtest/"))
    changed_engine["source_hashes"][engine_path] = "0" * 64
    assert context(provenance=changed_engine)["comparison_key"] != key
    changed_environment = {**original["execution_provenance"], "environment_fingerprint": "0" * 64}
    assert context(provenance=changed_environment)["comparison_key"] != key
    incomplete = copy.deepcopy(original["execution_provenance"])
    del incomplete["source_hashes"]["src/quanto/backtest/engine.py"]
    assert context(provenance=incomplete)["comparison_key"] is None


def test_policy_evaluation_purpose_separates_direct_comparison(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "policy-purpose-context", "web")
    fixed = ContextStore(store).get(run.job_id)
    binding = BindingStore(store).get_asset_binding(fixed["dataset_version_id"])

    def policy(purpose):
        return create_context(kind="policy", dataset_id=fixed["dataset_version_id"],
            asset_binding=binding, ordered_symbols=fixed["backtest_spec"]["symbols"],
            backtest_spec=fixed["backtest_spec"], execution_provenance=fixed["execution_provenance"],
            input_hash=fixed["input_hash"], basket_revision_ref=fixed["basket_revision_ref"],
            name="PPO", method="PPO", evaluation_purpose=purpose)

    replay = policy("development_replay")
    selected = policy("post_selection_backtest")
    assert replay["evaluation_purpose"] == "development_replay"
    assert replay["comparison_key"] != selected["comparison_key"]
