from quanto.workbench.data_jobs import execute_data_job
from quanto.workbench.service import WorkbenchService
from quanto.sample_data import generate_sample_data
import json
import hashlib
from pathlib import Path
import pytest
import time
import pandas as pd
import zipfile

from quanto.data.loaders import load_dataset
import quanto.workbench.data_jobs as data_jobs
from tests.unit.test_fetcher_cache import FakeProvider
from tests.fixtures.backtest_cases import dataset_case


def _fake_feed_in_frozen_job(service, job_id, mode):
    """Install a deterministic test provider into this job's frozen import tree."""
    descriptor = service.store.get_job(job_id).input_path
    frozen = json.loads(Path(descriptor).read_text(encoding="utf-8"))
    root = Path(frozen["source_root"])
    fake = root / "src" / "akshare.py"
    source = '''import pandas as pd
def tool_trade_date_hist_sina():
    return pd.DataFrame({"trade_date": pd.bdate_range("2024-01-01", "2024-01-12")})
def fund_etf_hist_em(symbol, period, start_date, end_date, adjust):
    dates = pd.bdate_range("2024-01-01", "2024-01-03" if MODE == "prefix" else "2024-01-05")
    return pd.DataFrame({"日期": dates, "开盘": 1.0, "最高": 1.1, "最低": 0.9,
                         "收盘": 1.0, "成交量": 1000, "成交额": 10000})
def stock_zh_index_daily_em(symbol):
    dates = pd.bdate_range("2024-01-01", "2024-01-05")
    return pd.DataFrame({"date": dates, "close": 4000.0, "amount": 900000.0})
'''
    fake.write_text(f'MODE = "{mode}"\n' + source, encoding="utf-8")
    frozen["source_hashes"]["src/akshare.py"] = hashlib.sha256(fake.read_bytes()).hexdigest()
    Path(descriptor).write_text(json.dumps(frozen), encoding="utf-8")


@pytest.mark.parametrize("mode", ["complete", "prefix"])
def test_fetch_job_publishes_verified_outcome_through_scheduler(tmp_path, mode):
    service = WorkbenchService(tmp_path / "data")
    job_id = service.submit_job("fetch", {"symbols": ["000001"], "start_date": "2024-01-01",
        "end_date": "2024-01-05", "cache_version": mode, "request_delay_seconds": 0, "max_retries": 0})["id"]
    _fake_feed_in_frozen_job(service, job_id, mode)
    service.scheduler.start(background=False)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            service.scheduler.tick()
            job = service.get_resource("jobs", job_id)
            if job["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded", job
        if mode == "complete":
            assert job["result"]["state"] == "complete"
            assert job["result"]["dataset_version_id"] == job_id
            assert service.store.get_dataset(job_id).manifest["price_mode"] == "adjusted_research"
        else:
            assert job["result"]["state"] == "incomplete"
            assert job["result"]["coverage"]["000001"]["missing"] == ["2024-01-04", "2024-01-05"]
            with pytest.raises(KeyError):
                service.store.get_dataset(job_id)
    finally:
        service.scheduler.stop()


def test_calendar_provider_runtime_error_returns_incomplete_diagnostic(tmp_path, monkeypatch):
    class Broken(FakeProvider):
        def fetch_calendar(self, start, end):
            raise RuntimeError("provider unavailable")
    monkeypatch.setattr(data_jobs, "AKShareDataProvider", Broken)
    result = execute_data_job("fetch", {"workspace": str(tmp_path), "symbols": ["000001"],
        "start_date": "2024-01-01", "end_date": "2024-01-05"}, lambda _: None, lambda: False)
    assert result == {"state": "incomplete", "dataset_version_id": None, "coverage": {},
                      "reason": "CALENDAR_INCOMPLETE", "error": "RuntimeError"}


def test_calendar_provider_error_during_cancellation_is_not_incomplete(tmp_path, monkeypatch):
    class Broken(FakeProvider):
        def fetch_calendar(self, start, end):
            raise RuntimeError("provider interrupted")
    monkeypatch.setattr(data_jobs, "AKShareDataProvider", Broken)
    calls = iter([False, True])
    with pytest.raises(RuntimeError, match="CANCELLED"):
        execute_data_job("fetch", {"workspace": str(tmp_path), "symbols": ["000001"],
            "start_date": "2024-01-01", "end_date": "2024-01-05"}, lambda _: None, lambda: next(calls))


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


def test_import_nested_auxiliary_table_preserves_registered_file_hashes(tmp_path):
    source = dataset_case(tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    manifest["auxiliary_tables"] = {"signals": "aux/signals.csv"}
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = tmp_path / "dataset.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for file in source.iterdir():
            output.write(file, file.name)
        output.writestr("aux/signals.csv", "available_at,symbol,score\n2024-01-01T00:00:00Z,000001,1\n")
    service = WorkbenchService(tmp_path / "data")
    upload_id = service.upload("dataset.zip", archive.read_bytes())
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
        dataset = service.store.get_dataset(identity)
        from quanto.workbench.revisions import digest, json_bytes, source_files
        hashes = {name: digest(content) for name, content in source_files(dataset.path).items()}
        assert "aux/signals.csv" in hashes
        assert dataset.content_hash == digest(json_bytes(hashes))
        assert "signals" in load_dataset(dataset.path).auxiliary
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
