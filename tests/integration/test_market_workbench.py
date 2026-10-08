import json
from pathlib import Path

import pytest

import quanto.workbench.data_jobs as jobs
from quanto.data.settings import configure_market_root
from quanto.workbench.service import WorkbenchService
from tests.fixtures.market_dataset_cases import research_market


PAYLOAD = {"instruments": [{"exchange": "SZSE", "instrument_type": "etf", "code": "000001"}], "start_date": "2024-01-01", "end_date": "2024-01-05"}


def prepared_job(tmp_path, monkeypatch, mode="complete"):
    service = WorkbenchService(tmp_path / "workspace")
    identity = service.submit_job("fetch", PAYLOAD)["id"]
    frozen = json.loads(Path(service.store.get_job(identity).input_path).read_text(encoding="utf-8"))["input"]
    assert frozen["market_root"] == str(service.store.market_root)
    monkeypatch.setattr(jobs, "MarketData", lambda root, **kwargs: research_market(root, mode))
    output = service._job_root(identity) / "final"
    result = jobs.execute_data_job("fetch", {**frozen, "_output_dir": str(output)}, lambda _: None, lambda: False)
    with service.store.transaction() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (identity,))
    return service, identity, output, result


def test_market_provenance_commits_with_dataset(tmp_path, monkeypatch):
    service, identity, output, result = prepared_job(tmp_path, monkeypatch)
    assert result["market_prefetch_state"] == "ready"
    assert service.store.finish_data_job(identity, output, result)
    provenance = service.store.dataset_market_source(identity)
    assert provenance == {key: result["market_snapshot_ref"][key] for key in ("id", "manifest_digest")}
    assert service.store.get_dataset(identity).manifest["price_mode"] == "adjusted_research"


def test_workbench_cancel_after_market_publication_blocks_registration(tmp_path, monkeypatch):
    service, identity, output, result = prepared_job(tmp_path, monkeypatch)
    service.store.request_cancel(identity)
    assert not service.store.finish_data_job(identity, output, result)
    assert service.store.dataset_market_source(identity) is None
    with pytest.raises(KeyError):
        service.store.get_dataset(identity)
    with research_market(service.store.market_root) as market:
        assert market.catalog.resolve_snapshot(result["market_snapshot_ref"]["id"])


def test_succeeded_incomplete_fetch_cannot_register_dataset(tmp_path, monkeypatch):
    service, identity, output, result = prepared_job(tmp_path, monkeypatch, "prefix")
    assert result["state"] == "incomplete"
    assert service.store.finish_data_job(identity, output, result)
    assert service.store.get_job(identity).state == "succeeded"
    with pytest.raises(KeyError):
        service.store.get_dataset(identity)
    assert service.store.dataset_market_source(identity) is None


def test_ready_market_snapshot_does_not_bypass_bundle_validation(tmp_path, monkeypatch):
    service, identity, output, result = prepared_job(tmp_path, monkeypatch)
    (output / "status.parquet").unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        service.store.finish_data_job(identity, output, result)
    with pytest.raises(KeyError):
        service.store.get_dataset(identity)


def test_claimed_ready_with_unknown_snapshot_cannot_register(tmp_path, monkeypatch):
    service, identity, output, result = prepared_job(tmp_path, monkeypatch)
    result["market_snapshot_ref"]["id"] = "0" * 64
    with pytest.raises(ValueError, match="INVALID_OUTPUT"):
        service.store.finish_data_job(identity, output, result)
    assert service.store.dataset_market_source(identity) is None


def test_registration_uses_frozen_market_root(tmp_path, monkeypatch):
    service, identity, output, result = prepared_job(tmp_path, monkeypatch)
    configure_market_root(tmp_path / "changed_after_submission")
    assert service.store.finish_data_job(identity, output, result)
    assert not (tmp_path / "changed_after_submission").exists()


@pytest.mark.parametrize("key", ["cache_version", "provider", "root", "market_root", "max_retries", "request_delay_seconds", "calendar"])
def test_fetch_rejects_provider_and_cache_controls(tmp_path, key):
    with pytest.raises(ValueError):
        WorkbenchService(tmp_path).submit_job("fetch", {**PAYLOAD, key: "injected"})
