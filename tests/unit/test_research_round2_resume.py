"""A resumed study must never silently reuse another protocol's results."""

import hashlib
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    return importlib.import_module("research_round2")


def test_resume_accepts_only_matching_identity_and_unchanged_curve(runner, tmp_path):
    expected = {"candidate": "example", "period": "full", "cost_multiplier": 2.,
                "protocol_sha256": "frozen", "parameters": {"kind": "example"}, "spec": {"seed": 7}}
    assert runner.load_cached(tmp_path, expected) is None
    curve = tmp_path / "curves.csv"
    curve.write_text("date,equity\n2020-01-02,100\n", encoding="utf-8")
    row = {**expected, "curves_sha256": hashlib.sha256(curve.read_bytes()).hexdigest(), "metrics": {"total_return": 0}}
    (tmp_path / "summary.json").write_text(json.dumps(row), encoding="utf-8")
    assert runner.load_cached(tmp_path, expected) == row
    for name, value in (("protocol_sha256", "changed"), ("cost_multiplier", 1.), ("candidate", "other")):
        with pytest.raises(ValueError, match="Cached"):
            runner.load_cached(tmp_path, {**expected, name: value})
    curve.write_text("date,equity\n2020-01-02,200\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Cached"):
        runner.load_cached(tmp_path, expected)
    curve.unlink()
    with pytest.raises(ValueError, match="Cached"):
        runner.load_cached(tmp_path, expected)
