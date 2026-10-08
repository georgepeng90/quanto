from uuid import uuid4
import zipfile
import json

import pandas as pd
import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.workbench.data_jobs import execute_data_job
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.backtest_cases import dataset_case
from tests.fixtures.framework_imports import fixture_identity_evidence


TABLES = ("prices", "benchmark", "calendar", "instruments", "status", "events")


def assert_parquet_bundle(path):
    assert all((path / f"{name}.parquet").is_file() for name in TABLES)
    assert not list(path.rglob("*.csv"))
    bundle = load_dataset(path)
    assert bundle.prices.symbol.iloc[0] == "SZSE:etf:000001"
    return bundle


def test_new_registration_canonicalizes_without_changing_import(tmp_path):
    source = dataset_case(tmp_path / "source")
    original = {p.name: p.read_bytes() for p in source.iterdir()}
    expected = load_dataset(source)
    version = WorkbenchStore(tmp_path / "workbench").register_dataset(
        source, identity_evidence=fixture_identity_evidence(source))
    actual = assert_parquet_bundle(version.path)
    expected.prices["symbol"] = "SZSE:etf:000001"
    pd.testing.assert_frame_equal(actual.prices, expected.prices)
    # Arrow stores second-resolution timestamps at millisecond resolution without changing values.
    pd.testing.assert_frame_equal(actual.events, expected.events, check_dtype=False)
    assert {p.name: p.read_bytes() for p in source.iterdir()} == original
    assert version.manifest == actual.manifest.model_dump(mode="json")


def test_import_job_publishes_only_parquet_tables(tmp_path):
    source = dataset_case(tmp_path / "source")
    upload = tmp_path / "upload.zip"
    with zipfile.ZipFile(upload, "w") as archive:
        for file in source.iterdir():
            archive.write(file, file.name)
        archive.writestr("identity_evidence.json", json.dumps(fixture_identity_evidence(source)))
    destination = tmp_path / "output"
    result = execute_data_job("import", {"workspace": str(tmp_path), "_output_dir": str(destination),
        "upload_id": str(uuid4()), "_upload_path": str(upload), "_source_root": str(tmp_path)},
        lambda _: None, lambda: False)
    assert result["state"] == "complete"
    assert_parquet_bundle(destination)


def test_demo_job_publishes_only_parquet_tables(tmp_path):
    destination = tmp_path / "output"
    result = execute_data_job("demo", {"workspace": str(tmp_path), "_output_dir": str(destination), "years": 1},
        lambda _: None, lambda: False)
    assert result["state"] == "complete"
    assert all((destination / f"{name}.parquet").is_file() for name in TABLES)
    assert not list(destination.rglob("*.csv"))
    assert load_dataset(destination).manifest.is_demo


def test_registration_conversion_failure_does_not_register_partial_version(tmp_path, monkeypatch):
    source = dataset_case(tmp_path / "source")
    store = WorkbenchStore(tmp_path / "workbench")
    def failed_write(*args, **kwargs):
        raise OSError("full disk")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", failed_write)
    with pytest.raises(OSError, match="full disk"):
        store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM datasets").fetchone()[0] == 0
