"""Workbench service exposes evidence and freezes strict framework submissions."""
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from quanto.objects import digest, json_bytes
from quanto.learning.registry import hash_files
from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.framework_requests import FixedRequest, FetchRequest
from quanto.workbench.service import WorkbenchService
from tests.fixtures.backtest_cases import dataset_case
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.integration.test_snapshot_submission import _ready


FETCH = {"instruments": [{"exchange": "SZSE", "instrument_type": "etf", "code": "000001"}],
         "start_date": "2024-01-01", "end_date": "2024-01-05"}


def test_dataset_detail_exposes_current_asset_identities(tmp_path):
    service = WorkbenchService(tmp_path / "workspace")
    source = dataset_case(tmp_path / "source")
    dataset = service.store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    detail = service.dataset_detail(dataset.id)
    assert detail["asset_binding"]["dataset_version_id"] == dataset.id
    assert detail["instruments"][0]["instrument_id"]["instrument_type"] == "etf"


def test_dataset_without_current_binding_is_rejected(tmp_path):
    service = WorkbenchService(tmp_path / "workspace")
    source = dataset_case(tmp_path / "source")
    identity = str(uuid4())
    target = service.store.root / "datasets" / identity
    target.parent.mkdir(exist_ok=True)
    shutil.copytree(source, target)
    files = hash_files(target)
    with service.store.transaction() as db:
        db.execute("INSERT INTO datasets VALUES (?,?,?,?)", (identity, str(target), digest(json_bytes(files)),
                   (target / "manifest.json").read_text(encoding="utf-8")))
    with pytest.raises(KeyError):
        service.dataset_detail(identity)


def test_clone_new_run_uses_frozen_public_request_with_source(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    service = WorkbenchService(store.root)
    run = service.runs.submit(draft.id, draft.revision, "source", "web")
    cloned = service.clone_run(run.run_id)
    request = FixedRequest.model_validate(cloned["payload"])
    assert request.source_run_id == run.run_id
    assert "symbols" not in cloned["payload"]["backtest_spec"]
    assert request.strategy_params == FixedRequest.model_validate(draft.payload).strategy_params


def test_clone_rejects_replaced_frozen_input_when_context_still_points_to_original(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    service = WorkbenchService(store.root)
    run = service.runs.submit(draft.id, draft.revision, "source", "web")
    manifest = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 2
    manifest.pop("resolved_configuration")
    manifest.pop("framework_contract_version")
    content = json_bytes(manifest)
    run.manifest_path.write_bytes(content)
    with store.transaction() as db:
        db.execute("UPDATE runs SET manifest_hash=? WHERE run_id=?", (digest(content), run.run_id))
    with pytest.raises(ValueError, match="frozen input reference differs"):
        service.clone_run(run.run_id)


def test_migrated_historical_run_clones_as_current_draft_without_rewriting_results(tmp_path):
    from quanto.workbench.bindings import BindingStore
    from quanto.workbench.run_contexts import ContextStore, fixed_context_from_manifest

    store, draft, _, _ = _ready(tmp_path)
    service = WorkbenchService(store.root)
    run = service.runs.submit(draft.id, draft.revision, "historical-source", "web")
    manifest = json.loads(run.manifest_path.read_bytes())
    manifest["schema_version"] = 2
    manifest.pop("resolved_configuration")
    manifest.pop("framework_contract_version")
    raw = json_bytes(manifest)
    run.manifest_path.write_bytes(raw)
    manifest_hash = digest(raw)
    binding = BindingStore(store).get_asset_binding(manifest["dataset_version_id"])
    context = fixed_context_from_manifest(manifest, input_hash=manifest_hash,
        asset_binding=binding, basket_revision_ref=draft.payload["basket_revision_ref"])
    with store.transaction() as db:
        db.execute("UPDATE runs SET manifest_hash=? WHERE run_id=?", (manifest_hash, run.run_id))
        db.execute("DELETE FROM run_contexts WHERE job_id=?", (run.job_id,))
        ContextStore(store).insert(db, run.job_id, context)

    cloned = service.clone_run(run.run_id)
    request = FixedRequest.model_validate(cloned["payload"])
    assert request.source_run_id == run.run_id
    assert request.basket_revision_ref.model_dump(mode="json") == draft.payload["basket_revision_ref"]
    assert request.backtest_spec.to_spec(binding.resolve(tuple(
        item.instrument_id for item in binding.assets))).model_dump(mode="json")["symbols"] == manifest["backtest_spec"]["symbols"]
    assert run.manifest_path.read_bytes() == raw


def test_fetch_freezes_normalized_request_and_capability_report(tmp_path):
    service = WorkbenchService(tmp_path / "workspace")
    job = service.submit_job("fetch", FETCH)
    frozen = json.loads(Path(service.store.get_job(job["id"]).input_path).read_text(encoding="utf-8"))
    framework = frozen["input"]["framework"]
    request = FetchRequest.model_validate(framework["request"])
    report = FrameworkResolver(service.store).check(request)
    assert framework["request"] == request.model_dump(mode="json")
    assert framework["capability_report"]["request_hash"] == report.request_hash
    assert frozen["input"]["refresh"] is False


def test_fetch_final_capability_change_rolls_back_and_removes_only_new_input(tmp_path, monkeypatch):
    service = WorkbenchService(tmp_path / "workspace")
    prior = service.submit_job("demo", {"years": 1})
    prior_dir = service.store.root / "job-inputs" / prior["id"]
    original = FrameworkResolver.check
    calls = 0
    def changed(self, request, *, db=None):
        nonlocal calls
        report = original(self, request, db=db)
        calls += 1
        return report if calls == 1 else report.model_copy(update={"request_hash": "f" * 64})
    monkeypatch.setattr(FrameworkResolver, "check", changed)
    with pytest.raises(ValueError, match="conflict|changed"):
        service.submit_job("fetch", FETCH)
    assert calls == 2
    assert prior_dir.is_dir()
    assert len(service.store.list_jobs()) == 1
    assert {item.name for item in (service.store.root / "job-inputs").iterdir()} == {prior["id"]}


def test_blocked_fetch_creates_no_job_or_input(tmp_path):
    service = WorkbenchService(tmp_path / "workspace")
    with pytest.raises(ValueError, match="DATA_REQUIREMENT_UNSATISFIED"):
        service.submit_job("fetch", {**FETCH, "auxiliary": {"unknown": {"columns": ["x"]}}})
    assert service.store.list_jobs() == []
    assert not list((service.store.root / "job-inputs").glob("*"))
