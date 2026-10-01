import json
from pathlib import Path
import subprocess
import sys

import pytest

from quanto.data import MarketData
from quanto.data.settings import configure_market_root, get_market_root
from quanto.workbench.store import WorkbenchStore


def test_default_root_is_owned_by_user_not_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("QUANTO_MARKET_CONFIG")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    assert get_market_root() == tmp_path / ".quanto" / "market"


def test_all_workspaces_and_default_facade_share_configured_root(tmp_path):
    root = configure_market_root(tmp_path / "shared")
    first = WorkbenchStore(tmp_path / "first")
    second = WorkbenchStore(tmp_path / "second")
    assert first.market_root == second.market_root == root
    with MarketData(providers=()) as market:
        assert market.store.root == root
    assert "market_root" not in json.loads((first.root / "workspace.json").read_text())


def test_workspace_cannot_override_global_root(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "workspace.json").write_text(json.dumps({"artifact_root": "artifacts", "market_root": "old-local"}))
    assert WorkbenchStore(workspace).market_root == get_market_root()
    assert not (workspace / "old-local").exists()


def test_child_processes_use_same_library_from_different_directories(tmp_path):
    root = configure_market_root(tmp_path / "shared")
    for name in ("checkout-a", "checkout-b"):
        cwd = tmp_path / name
        cwd.mkdir()
        result = subprocess.run([sys.executable, "-c", "from quanto.data.settings import get_market_root; print(get_market_root())"], cwd=cwd, check=True, capture_output=True, text=True)
        assert Path(result.stdout.strip()) == root


def test_relative_root_cannot_depend_on_current_directory(isolated_market_config):
    with pytest.raises(ValueError, match="absolute"):
        configure_market_root(Path("market"))
    isolated_market_config.write_text(json.dumps({"root": "market"}))
    with pytest.raises(ValueError, match="absolute"):
        get_market_root()
