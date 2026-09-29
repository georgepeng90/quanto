from quanto.workbench.data_jobs import execute_data_job
from quanto.workbench.service import WorkbenchService
from quanto.sample_data import generate_sample_data
import json
import pytest
import time
import pandas as pd
import zipfile

from quanto.data.loaders import load_dataset
import quanto.workbench.data_jobs as data_jobs
from tests.unit.test_fetcher_cache import FakeProvider
from tests.fixtures.backtest_cases import dataset_case


def test_incomplete_fetch_is_not_registered_as_complete(tmp_path):
    result = execute_data_job("fetch", {"workspace": str(tmp_path), "symbols": ["512480"], "start_date": "2024-01-01", "end_date": "2024-01-03", "calendar": []}, lambda _: None, lambda: False)
    assert result["state"] == "incomplete"
    assert result["dataset_version_id"] is None


def test_cancel_winning_publication_does_not_register_dataset(tmp_path):
    service = WorkbenchService(tmp_path / "data")
    job = service.submit_job("demo", {"years": 1})
    service.store.request_cancel(job["id"])
    final = service._job_root(job["id"]) / "final"
    generate_sample_data(final, years=1)
    assert not service.store.finish_data_job(job["id"], final, {"state": "complete"})
    with pytest.raises(KeyError):
        service.store.get_dataset(job["id"])


def test_demo_job_uses_shared_scheduler_and_publishes_dataset(tmp_path):
    service = WorkbenchService(tmp_path / "data")
    service.scheduler.start(background=False)
    try:
        identity = service.submit_job("demo", {"years": 1})["id"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            service.scheduler.tick()
            job = service.get_resource("jobs", identity)
            if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded", job
        assert job["result"]["dataset_version_id"] == identity
        assert service.store.get_dataset(identity).manifest["is_demo"] is True
    finally:
        service.scheduler.stop()


def test_complete_fake_qfq_feed_maps_to_research_bundle(tmp_path, monkeypatch):
    class Feed(FakeProvider):
        def fetch_calendar(self, start, end):
            return [day.date().isoformat() for day in pd.bdate_range("2024-01-01", "2024-01-08")]
    monkeypatch.setattr(data_jobs, "AKShareDataProvider", Feed)
    output = tmp_path / "bundle"
    payload = {"workspace": str(tmp_path), "_output_dir": str(output), "symbols": ["000001"],
               "start_date": "2024-01-01", "end_date": "2024-01-05", "cache_version": "fake-v1",
               "request_delay_seconds": 0, "max_retries": 0}
    result = execute_data_job("fetch", payload, lambda _: None, lambda: False)
    assert result["state"] == "complete"
    bundle = load_dataset(output)
    assert bundle.price_mode == "adjusted_research"
    assert bundle.prices.symbol.iloc[0] == "000001"
    assert bundle.manifest.calendar_end.isoformat() == "2024-01-08"
    assert "cache version fake-v1" in bundle.manifest.source
    assert any("研究模型假设" in item for item in bundle.manifest.limitations)


def test_prefix_only_feed_is_incomplete(tmp_path, monkeypatch):
    class Prefix(FakeProvider):
        def fetch_calendar(self, start, end):
            return [day.date().isoformat() for day in pd.bdate_range("2024-01-01", "2024-01-08")]
        def fetch_etf_daily(self, symbol, start, end):
            return super().fetch_etf_daily(symbol, start, "2024-01-03")
    monkeypatch.setattr(data_jobs, "AKShareDataProvider", Prefix)
    result = execute_data_job("fetch", {"workspace": str(tmp_path), "_output_dir": str(tmp_path / "bundle"),
                              "symbols": ["000001"], "start_date": "2024-01-01", "end_date": "2024-01-05",
                              "cache_version": "fake-v1", "request_delay_seconds": 0, "max_retries": 0},
                              lambda _: None, lambda: False)
    assert result["state"] == "incomplete"
    assert result["coverage"]["000001"]["missing"] == ["2024-01-04", "2024-01-05"]
    assert not (tmp_path / "bundle" / "manifest.json").exists()


def test_import_job_uses_frozen_upload_and_registers_raw_cash(tmp_path):
    source = dataset_case(tmp_path / "source")
    archive = tmp_path / "dataset.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for file in source.iterdir():
            output.write(file, file.name)
    service = WorkbenchService(tmp_path / "data")
    upload_id = service.upload("dataset.zip", archive.read_bytes())
    preflight = service.preflight_upload(upload_id)
    assert preflight["price_mode"] == "raw_cash"
    assert preflight["validation"] == "metadata_only"
    identity = service.submit_job("import", {"upload_id": upload_id})["id"]
    service.scheduler.start(background=False)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            service.scheduler.tick()
            job = service.get_resource("jobs", identity)
            if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded", job
        assert service.store.get_dataset(identity).manifest["price_mode"] == "raw_cash"
    finally:
        service.scheduler.stop()


def test_strategy_validation_imports_user_code_only_in_worker(tmp_path):
    service = WorkbenchService(tmp_path / "data")
    declaration = {"entrypoint": "strategy.py:create_strategy", "parameter_schema": {"type": "object", "properties": {}},
                   "auxiliary_sources": [], "research_data": []}
    source = "class Strategy:\n    warmup_bars = 0\n    def should_evaluate(self, ctx): return True\n    def on_close(self, ctx): return None\ndef create_strategy(params): return Strategy()\n"
    version = service.store.register_strategy({"strategy.json": json.dumps(declaration).encode(),
                                                "strategy.py": source.encode()}, declaration)
    identity = service.submit_job("validate_strategy", {"strategy_revision_id": version.id, "params": {}})["id"]
    assert service.store.get_job(identity).state == "queued"
    service.scheduler.start(background=False)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            service.scheduler.tick()
            job = service.get_resource("jobs", identity)
            if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded", job
        assert job["result"] == {"valid": True, "warmup_bars": 0}
    finally:
        service.scheduler.stop()
