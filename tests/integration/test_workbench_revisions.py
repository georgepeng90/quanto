import json

import pandas as pd
import pytest

from quanto.data.loaders import load_dataset
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.backtest_cases import dataset_case


def test_draft_revision_conflict_keeps_first_save(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench")
    first = store.save_experiment(None, None, {"name": "first"})
    saved = store.save_experiment(first.id, first.revision, {"name": "second"})
    with pytest.raises(ValueError, match="revision"):
        store.save_experiment(first.id, first.revision, {"name": "third"})
    assert saved.revision == 2
    assert store.get_experiment(first.id).payload == {"name": "second"}


def test_registered_parquet_dataset_preserves_leading_zero_symbol(tmp_path):
    source = dataset_case(tmp_path / "source")
    for name in ("prices", "benchmark", "calendar", "instruments", "status", "events"):
        path = source / f"{name}.csv"
        pd.read_csv(path, dtype={"symbol": str}).to_parquet(source / f"{name}.parquet", index=False)
        path.unlink()
    store = WorkbenchStore(tmp_path / "workbench")
    version = store.register_dataset(source)
    loaded = load_dataset(version.path)
    assert loaded.prices.iloc[0].symbol == "000001"
    assert loaded.instruments.iloc[0].symbol == "000001"
    assert loaded.price_mode == "raw_cash"
    assert json.loads((version.path / "manifest.json").read_text())["data_start"] == "2024-01-01"


def test_dataset_registration_excludes_unrelated_secret_file(tmp_path):
    source = dataset_case(tmp_path / "source")
    (source / ".env").write_text("TOKEN=private")
    store = WorkbenchStore(tmp_path / "workbench")
    version = store.register_dataset(source)
    assert not (version.path / ".env").exists()
    assert (source / ".env").read_text() == "TOKEN=private"


@pytest.mark.parametrize("bad", ["../escape.py", "/absolute.py", "C:/absolute.py"])
def test_strategy_registration_rejects_unsafe_paths(tmp_path, bad):
    store = WorkbenchStore(tmp_path / "workbench")
    declaration = {"entrypoint": "strategy.py:create_strategy", "parameter_schema": {"type": "object", "properties": {}}, "auxiliary_sources": [bad], "research_data": []}
    files = {"strategy.json": json.dumps(declaration).encode(), "strategy.py": b"def create_strategy(params): pass\n", bad: b""}
    with pytest.raises(ValueError, match="unsafe"):
        store.register_strategy(files, declaration)


def test_strategy_registration_rejects_missing_declared_file(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench")
    declaration = {"entrypoint": "strategy.py:create_strategy", "parameter_schema": {"type": "object", "properties": {}}, "auxiliary_sources": ["helper.py"], "research_data": []}
    with pytest.raises(ValueError, match="missing"):
        store.register_strategy({"strategy.json": json.dumps(declaration).encode(), "strategy.py": b"def create_strategy(params): pass\n"}, declaration)


def test_strategy_registration_does_not_execute_source(tmp_path):
    store = WorkbenchStore(tmp_path / "workbench")
    declaration = {"entrypoint": "strategy.py:create_strategy", "parameter_schema": {"type": "object", "properties": {}}, "auxiliary_sources": [], "research_data": []}
    version = store.register_strategy({"strategy.json": json.dumps(declaration).encode(), "strategy.py": b"raise RuntimeError('must not execute')\n"}, declaration)
    assert (version.path / "strategy.py").read_bytes().startswith(b"raise RuntimeError")
