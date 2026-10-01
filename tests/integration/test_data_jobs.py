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

from quanto.backtest.dataset_loaders import load_dataset
import quanto.workbench.data_jobs as data_jobs
from tests.fixtures.market_dataset_cases import research_market
from tests.fixtures.backtest_cases import dataset_case


def _fake_feed_in_frozen_job(service, job_id, mode):
    """Install a deterministic test provider into this job's frozen import tree."""
    descriptor = service.store.get_job(job_id).input_path
    frozen = json.loads(Path(descriptor).read_text(encoding="utf-8"))
    root = Path(frozen["source_root"])
    fake = root / "src" / "akshare.py"
    source = '''import pandas as pd
def tool_trade_date_hist_sina():
    return pd.DataFrame({"trade_date": pd.bdate_range("2024-01-01", "2024-01-31")})
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
        "end_date": "2024-01-05", "refresh": False})["id"]
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
            assert (service.store.get_dataset(job_id).path / "prices.parquet").exists()
        else:
            assert job["result"]["state"] == "incomplete"
            assert missing_days(job["result"]) == ["2024-01-04", "2024-01-05"]
            with pytest.raises(KeyError):
                service.store.get_dataset(job_id)
    finally:
        service.scheduler.stop()


def missing_days(result):
    entries = result["coverage"]["entries"]
    return sorted({stamp[:10] for entry in entries for instrument, stamps in entry["missing_by_instrument"].items()
                   if instrument.endswith("etf:000001") for stamp in stamps})


def test_provider_runtime_error_reports_failed_market_state(tmp_path, monkeypatch):
    monkeypatch.setattr(data_jobs, "MarketData", lambda root: research_market(root, "broken"))
    result = execute_data_job("fetch", {"workspace": str(tmp_path), "market_root": str(tmp_path / "market"), "symbols": ["000001"],
        "start_date": "2024-01-01", "end_date": "2024-01-05"}, lambda _: None, lambda: False)
    assert result["state"] == "incomplete"
    assert result["market_prefetch_state"] == "failed"
    assert result["dataset_version_id"] is None


def test_cancellation_during_market_preparation_is_not_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(data_jobs, "MarketData", lambda root: research_market(root))
    calls = iter([False, True])
    with pytest.raises(RuntimeError, match="CANCELLED"):
        execute_data_job("fetch", {"workspace": str(tmp_path), "market_root": str(tmp_path / "market"), "symbols": ["000001"],
            "start_date": "2024-01-01", "end_date": "2024-01-05"}, lambda _: None, lambda: next(calls))


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
    monkeypatch.setattr(data_jobs, "MarketData", lambda root: research_market(root))
    output = tmp_path / "bundle"
    result = execute_data_job("fetch", {"workspace": str(tmp_path), "market_root": str(tmp_path / "market"), "_output_dir": str(output),
        "symbols": ["000001"], "start_date": "2024-01-01", "end_date": "2024-01-05"}, lambda _: None, lambda: False)
    assert result["state"] == "complete"
    assert result["market_prefetch_state"] == "ready"
    bundle = load_dataset(output)
    assert bundle.price_mode == "adjusted_research"
    assert bundle.prices.symbol.iloc[0] == "000001"
    assert bundle.manifest.calendar_end.isoformat() > "2024-01-05"
    assert result["market_snapshot_ref"]["id"] in bundle.manifest.source
    assert bundle.manifest.limitations


def test_prefix_only_feed_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(data_jobs, "MarketData", lambda root: research_market(root, "prefix"))
    result = execute_data_job("fetch", {"workspace": str(tmp_path), "market_root": str(tmp_path / "market"), "_output_dir": str(tmp_path / "bundle"),
        "symbols": ["000001"], "start_date": "2024-01-01", "end_date": "2024-01-05"}, lambda _: None, lambda: False)
    assert result["state"] == "incomplete"
    assert result["market_prefetch_state"] == "incomplete"
    assert missing_days(result) == ["2024-01-04", "2024-01-05"]
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
        assert dataset.manifest["auxiliary_tables"]["signals"].endswith(".parquet")
        assert dataset.manifest["auxiliary_tables"]["signals"] in hashes
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
