"""Frozen Round 5 workbench publication boundaries."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from quanto.workbench.store import WorkbenchStore
from scripts import register_round5_results as publish


CONFIG = {"kind": "breadth_risk", "params": {
    "entry_breadth": .55, "exit_breadth": .45, "target_vol": .11,
    "vol_window": 60, "gross_target": .8}}


def test_real_bundle_imports_in_isolated_directory(tmp_path):
    sources = [(publish.PROJECT / name).read_text(encoding="utf-8") for name in publish.SOURCE_NAMES]
    manifest, files, expected_hash = publish.strategy_bundle("w60_v11", CONFIG, *sources)
    assert set(files) == {"strategy.json", "strategy.py", "scripts/__init__.py", *publish.SOURCE_NAMES}
    assert set(manifest["auxiliary_sources"]) == set(publish.SOURCE_NAMES) | {"scripts/__init__.py"}
    revision = WorkbenchStore(tmp_path / "workbench").register_strategy(
        {name: content.encode() for name, content in files.items()}, manifest)
    assert revision.content_hash == expected_hash
    code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2]); "
            "from strategy import create_strategy; "
            "strategy = create_strategy({'entry_breadth': .55, 'exit_breadth': .45, "
            "'target_vol': .11, 'vol_window': 60, 'gross_target': .8}); "
            "assert strategy.target_vol == .11 and strategy.vol_window == 60")
    result = subprocess.run([sys.executable, "-I", "-c", code, str(revision.path),
                             str(publish.PROJECT / "src")], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_selected_rows_allow_fewer_than_three_and_require_exact_identity(tmp_path):
    protocol = {"candidates": {"w60_v11": CONFIG},
                "periods": {"full": ["2023-10-09", "2026-09-30"],
                            "late": ["2026-01-05", "2026-09-30"]}}
    selection = {"selected": ["w60_v11"], "protocol_sha256": "frozen"}
    for period in ("full", "late"):
        path = tmp_path / "runs" / f"{period}__w60_v11__cost1" / "summary.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"candidate": "w60_v11", "period": period,
                                    "cost_multiplier": 1, "parameters": CONFIG,
                                    "protocol_sha256": "frozen", "spec": {"end": "2026-09-30"},
                                    "metrics": {"sharpe": 1.1}}))
    rows = publish.expected_rows(tmp_path, protocol, selection)
    assert set(rows) == {("w60_v11", "full", 1), ("w60_v11", "late", 1)}
    selection["selected"] = ["baseline"]
    with pytest.raises(ValueError, match="Invalid selected"):
        publish.expected_rows(tmp_path, protocol, selection)


def test_submission_persists_key_before_request_and_reuses_it(tmp_path):
    state_path = tmp_path / "state.json"
    item = {"experiment_id": "experiment", "expected_revision": 1,
            "idempotency_key": "fixed", "attempts": [{"idempotency_key": "fixed"}]}
    state = {"runs": [item]}
    calls = []

    def request(method, path, **kwargs):
        assert method == "POST" and path == "/api/runs"
        assert json.loads(state_path.read_text())["runs"][0]["idempotency_key"] == "fixed"
        calls.append(kwargs["json"])
        if len(calls) == 1:
            raise ConnectionError("response lost after submission")
        return {"run_id": "same-run"}

    with pytest.raises(ConnectionError):
        publish.submit_run(request, state, item, state_path)
    assert publish.submit_run(request, state, item, state_path) == "same-run"
    assert calls == [{"experiment_id": "experiment", "expected_revision": 1,
                      "idempotency_key": "fixed", "source": "agent"}] * 2
    assert item["attempts"] == [{"idempotency_key": "fixed", "run_id": "same-run"}]


def test_experiment_payload_matches_effective_parameters(tmp_path):
    state_path = tmp_path / "state.json"
    row = {"parameters": CONFIG, "spec": {"end": "2026-09-30"}}
    item = {"candidate": "w60_v11", "period": "full", "dataset_version_id": "dataset",
            "strategy_revision_id": "revision"}
    payload = publish.experiment_payload(item, row)
    assert payload["strategy_params"] == CONFIG["params"]
    canonical = {**payload, "effective_strategy_params": CONFIG["params"]}
    state = {"runs": [item]}

    def request(method, path, **kwargs):
        assert method == "GET" and path == "/api/experiments"
        return {"items": [{"id": "existing", "revision": 2, "payload": canonical}],
                "next_cursor": None}

    publish.ensure_experiment(request, state, item, row, state_path)
    assert (item["experiment_id"], item["expected_revision"]) == ("existing", 2)


def test_exact_metric_dictionary_and_provenance_are_required():
    item = {"run_id": "run", "dataset_version_id": "dataset", "strategy_revision_id": "revision",
            "attempts": [{"idempotency_key": "fixed"}]}
    row = {"metrics": {"sharpe": 1.0, "total_return": .2},
           "spec": {"end": "2026-09-30"}}

    def request(method, path):
        if path == "/api/runs/run":
            return {"state": "succeeded", "job_id": "job", "completed_through": "2026-09-30"}
        return {"metrics": {"sharpe": 1.0}, "metadata": {
            "dataset_version_id": "dataset", "strategy_revision_id": "revision"},
            "completed_through": "2026-09-30"}

    with pytest.raises(ValueError, match="Metric dictionary differs"):
        publish.verify_run(request, item, row)

    def wrong_revision(method, path):
        if path == "/api/runs/run":
            return {"state": "succeeded", "job_id": "job", "completed_through": "2026-09-30"}
        return {"metrics": row["metrics"], "metadata": {
            "dataset_version_id": "dataset", "strategy_revision_id": "other"},
            "completed_through": "2026-09-30"}

    with pytest.raises(ValueError, match="Workbench provenance differs"):
        publish.verify_run(wrong_revision, item, row)


def test_existing_dataset_hash_can_include_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "dataset_hash", lambda protocol: ("seven-file-hash", {"prices.parquet": "x"}))
    seen = []

    def verify(identity, api_hash, hashes):
        seen.append((identity, api_hash, hashes))
        return {"tables_exact": True}

    monkeypatch.setattr(publish, "verify_dataset", verify)
    state = {}
    assert publish.ensure_dataset(
        lambda method, path: {"content_hash": "whole-directory-with-receipt"}, state, {},
        tmp_path / "state.json", tmp_path / "docs") == publish.DATASET_ID
    assert seen == [(publish.DATASET_ID, "whole-directory-with-receipt", {"prices.parquet": "x"})]
    assert state["dataset_content_hash"] == "seven-file-hash"


def test_direct_script_imports_from_outside_checkout(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/register_round5_results.py"
    result = subprocess.run([sys.executable, "-I", "-c",
                             "import runpy, sys; runpy.run_path(sys.argv[1], run_name='import_probe')",
                             str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
