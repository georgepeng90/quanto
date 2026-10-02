"""Publication boundaries for the frozen Round 4 workbench results."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from quanto.config import validate_parameters
from quanto.workbench.store import WorkbenchStore
from scripts import register_round4_results as publish


def test_bundle_registers_nested_sources_and_imports_from_isolated_directory(tmp_path):
    round3 = "class Helper:\n    pass\n"
    round4 = "from scripts.research_round3_candidates import Helper\nclass BreadthRiskStrategy(Helper):\n    def __init__(self, entry_breadth):\n        self.entry_breadth = entry_breadth\n"
    manifest, files, expected_hash = publish.strategy_bundle(
        "h55_45", {"kind": "breadth_risk", "params": {"entry_breadth": .55}}, round4, round3)
    assert set(files) == {"strategy.json", "strategy.py", "scripts/__init__.py",
                          "scripts/research_round4_candidates.py", "scripts/research_round3_candidates.py"}
    assert set(manifest["auxiliary_sources"]) == set(files) - {"strategy.json", "strategy.py"}
    store = WorkbenchStore(tmp_path / "workbench")
    revision = store.register_strategy({name: content.encode() for name, content in files.items()}, manifest)
    assert revision.content_hash == expected_hash
    result = subprocess.run([sys.executable, "-I", "-c",
                             "import sys; sys.path.insert(0, sys.argv[1]); "
                             "from strategy import create_strategy; "
                             "assert create_strategy({'entry_breadth': .55}).entry_breadth == .55",
                             str(revision.path)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_real_candidate_bundle_imports_without_checkout_scripts(tmp_path):
    project = Path(__file__).resolve().parents[2]
    round4 = (project / "scripts/research_round4_candidates.py").read_text(encoding="utf-8")
    round3 = (project / "scripts/research_round3_candidates.py").read_text(encoding="utf-8")
    manifest, files, _ = publish.strategy_bundle(
        "h55_45", {"kind": "breadth_risk", "params": {"entry_breadth": .55,
                                                        "exit_breadth": .45}}, round4, round3)
    revision = WorkbenchStore(tmp_path / "workbench").register_strategy(
        {name: content.encode() for name, content in files.items()}, manifest)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2]); "
            "from strategy import create_strategy; "
            "strategy = create_strategy({'entry_breadth': .55, 'exit_breadth': .45}); "
            "assert strategy.entry_breadth == .55 and strategy.exit_breadth == .45; "
            "assert 'scripts.research_round4_candidates' in sys.modules")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(revision.path),
                             str(project / "src")], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_uncertain_submission_reuses_saved_idempotency_key(tmp_path):
    state_path = tmp_path / "state.json"
    item = {"experiment_id": "experiment", "expected_revision": 1,
            "idempotency_key": "fixed-key", "attempts": [{"idempotency_key": "fixed-key"}]}
    calls = []

    def uncertain(method, path, **kwargs):
        calls.append(kwargs["json"])
        assert json.loads(state_path.read_text())["runs"][0]["idempotency_key"] == "fixed-key"
        if len(calls) == 1:
            raise ConnectionError("response lost after commit")
        return {"run_id": "same-run"}

    state = {"runs": [item]}
    with pytest.raises(ConnectionError):
        publish.submit_run(uncertain, state, item, state_path)
    publish.submit_run(uncertain, state, item, state_path)
    assert calls == [{"experiment_id": "experiment", "expected_revision": 1,
                      "idempotency_key": "fixed-key", "source": "agent"}] * 2
    assert state["runs"][0]["attempts"] == [{"idempotency_key": "fixed-key", "run_id": "same-run"}]


def test_failed_attempt_is_preserved_and_prevents_resubmission(tmp_path):
    item = {"run_id": "failed-run", "attempts": [{"idempotency_key": "key", "run_id": "failed-run",
                                                   "state": "failed", "error": "bad run"}]}
    with pytest.raises(RuntimeError, match="failed-run"):
        publish.submit_run(lambda *args, **kwargs: pytest.fail("unexpected submission"),
                           {"runs": [item]}, item, tmp_path / "state.json")


def test_optional_volatility_parameter_accepts_null():
    manifest, _, _ = publish.strategy_bundle(
        "h55_45", {"kind": "breadth_risk", "params": {"target_vol": None}}, "", "")
    assert validate_parameters(manifest["parameter_schema"], {"target_vol": None}) == {"target_vol": None}


def test_experiment_resume_matches_canonical_effective_parameters(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "STATE", tmp_path / "state.json")
    row = {"parameters": {"params": {"entry_breadth": .55}}, "spec": {"end": "2026-09-30"}}
    item = {"candidate": "one", "period": "full", "dataset_version_id": "data",
            "strategy_revision_id": "strategy"}
    payload = publish.experiment_payload(item, row)
    canonical = {**payload, "effective_strategy_params": {"entry_breadth": .55}}
    state = {"runs": [item]}

    def request(method, path, **kwargs):
        assert method == "GET" and path == "/api/experiments"
        return {"items": [{"id": "existing", "revision": 1, "payload": canonical}], "next_cursor": None}

    publish.ensure_experiment(request, state, item, row)
    assert item["experiment_id"] == "existing"


def test_selected_rows_reject_changed_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "ROOT", tmp_path)
    protocol = {"candidates": {name: {"kind": "breadth_risk", "params": {}}
                               for name in ("one", "two", "three")},
                "families": {"hysteresis": ["one"], "volatility": ["two"], "combined": ["three"]}}
    selection = {"protocol_sha256": "frozen", "selected_by_family": {
        "hysteresis": "one", "volatility": "two", "combined": "three"},
        "selected": ["one", "two", "three"]}
    path = tmp_path / "runs/full__one__cost1/summary.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"candidate": "other", "period": "full", "cost_multiplier": 1,
                                "parameters": {"kind": "breadth_risk", "params": {}},
                                "protocol_sha256": "frozen", "spec": {"end": "2026-09-30"},
                                "metrics": {"sharpe": 1}}))
    with pytest.raises(ValueError, match="identity mismatch"):
        publish.expected_rows(protocol, selection)
