import ast
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def test_data_module_has_no_consumer_imports():
    prohibited = ("quanto.config", "quanto.backtest", "quanto.universe", "quanto.workbench", "torch", "tensorflow")
    for path in (ROOT / "src/quanto/data").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_bytes())):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(name == prefix or name.startswith(prefix + ".") for name in names for prefix in prohibited), path


def test_market_root_is_git_ignored():
    for path in ("data/market/catalog.sqlite", "data/market/objects/ab/xx.parquet", "data/market/evidence/ab/xx.bin", "market/snapshots/id/manifest.json"):
        assert subprocess.run(["git", "check-ignore", "--quiet", path], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "check-ignore", "--quiet", "tests/fixtures/market_sources/new-example.json"], cwd=ROOT).returncode == 1
