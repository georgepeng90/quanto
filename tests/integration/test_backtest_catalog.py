"""The backtest catalog reads one stored context for each submitted run."""
from __future__ import annotations

import pytest

from quanto.workbench.backtest_catalog import catalog
from quanto.workbench.runs import RunService
from quanto.workbench.service import WorkbenchService
from tests.integration.test_snapshot_submission import _ready
from tests.integration.test_worker_snapshot import ready_run, run_until_finished


def test_catalog_pages_submitted_runs_and_reads_current_result_state(tmp_path):
    store, first, _ = ready_run(tmp_path)
    dataset = store.get_dataset(store.get_experiment(first.experiment_id).payload["dataset_version_id"])
    second = RunService(store).submit(first.experiment_id, first.experiment_revision, "second-catalog", "web")
    workbench = WorkbenchService(store.root)

    page = catalog(workbench, limit=1)
    assert [item["id"] for item in page["items"]] == [first.run_id]
    assert page["items"][0]["metrics"] is None
    assert page["items"][0]["canonical_assets"] == ["SZSE:etf:000001"]
    assert page["items"][0]["dataset_source"] == dataset.manifest["source"]
    assert page["next_cursor"] is not None
    following = catalog(workbench, cursor=page["next_cursor"], limit=1)
    assert [item["id"] for item in following["items"]] == [second.run_id]
    assert following["next_cursor"] is None

    assert run_until_finished(store, first.job_id).state == "succeeded"
    completed = catalog(workbench, limit=1)["items"][0]
    assert completed["state"] == "succeeded"
    assert completed["metrics"]
    assert completed["result_error"] is None
    assert completed["comparison_key"] == page["items"][0]["comparison_key"]


def test_catalog_rejects_missing_context_and_changed_frozen_input(tmp_path):
    store, draft, _, _ = _ready(tmp_path)
    run = RunService(store).submit(draft.id, draft.revision, "context-required", "web")
    workbench = WorkbenchService(store.root)
    with store.transaction() as db:
        db.execute("DELETE FROM run_contexts WHERE job_id=?", (run.job_id,))
    with pytest.raises(ValueError, match="context missing"):
        catalog(workbench)

    replay = RunService(store).submit(draft.id, draft.revision, "context-check", "web")
    replay.manifest_path.write_bytes(replay.manifest_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="frozen input changed"):
        catalog(workbench, cursor="1")
