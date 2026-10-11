"""Published results retain the frozen identity used during execution."""

import csv
import json
import time
from pathlib import Path

import pytest

from quanto.investing.models import InvestingModels
from quanto.investing.jobs import _canonical_csv_rows, _execution_provenance
from quanto.learning.artifacts import read_manifest
from quanto.data.contracts import InstrumentId
from types import SimpleNamespace
from tests.integration.test_investing_models import model_workspace
from tests.integration.test_worker_snapshot import ready_run, run_until_finished


def test_fixed_report_exports_frozen_framework_and_canonical_instruments(tmp_path):
    store, run, _ = ready_run(tmp_path)
    frozen = json.loads(run.manifest_path.read_bytes())
    assert run_until_finished(store, run.job_id).state == "succeeded"
    final = store.artifact_root / run.job_id / "final"
    metadata = json.loads((final / "metadata.json").read_bytes())
    assert metadata["schema_version"] == "3"
    assert metadata["framework"] == frozen["resolved_configuration"]
    assert metadata["execution_provenance"]["source_hashes"] == frozen["files"]
    assert metadata["execution_provenance"]["basket_selection_note"] == "fixture selection"
    for name in ("orders", "fills", "positions", "account_events", "matched_trades", "open_lots"):
        with (final / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream)
            assert "instrument_id" in rows.fieldnames
            assert all(row["instrument_id"] == "SZSE:etf:000001" if row["symbol"] else not row["instrument_id"]
                       for row in rows)
    with (final / "signals.csv").open(encoding="utf-8", newline="") as stream:
        assert "instrument_id" not in csv.DictReader(stream).fieldnames


def test_policy_artifacts_preserve_frozen_framework_and_selected_checkpoint(model_workspace):
    pytest.importorskip("stable_baselines3")
    app, client, headers, request = model_workspace
    service = InvestingModels(app.state.service)
    model = service.create_model(request)

    def finished(identity):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            job = app.state.service.store.get_job(identity)
            if job.state not in {"queued", "running", "cancel_requested"}:
                assert job.state == "succeeded", job.error
                return
            time.sleep(.05)
        pytest.fail("policy job did not finish")

    with client:
        submitted = service.train_model(model["id"], {"additional_timesteps": 8, "device": "cpu"})
        train_id = submitted["id"]
        frozen_train = json.loads(Path(app.state.service.store.get_job(train_id).input_path).read_bytes())
        finished(train_id)
        policy_root = app.state.service.store.artifact_root / train_id / "final" / "artifact"
        trained = read_manifest(policy_root, "policy")
        from quanto.investing.policy_versions import PolicyVersionStore
        version_context = PolicyVersionStore(app.state.service.store).get(train_id)
        assert version_context["framework_learning"] == trained["framework_learning"]
        assert trained["framework"] == frozen_train["input"]["resolved_configuration"]
        assert trained["execution_provenance"]["source_hashes"] == frozen_train["source_hashes"]
        assert trained["execution_provenance"]["best_checkpoint"]["timesteps"] == trained["best_timesteps"]
        assert trained["execution_provenance"]["last_checkpoint"]["cumulative_timesteps"] == trained["timesteps_completed"]
        assert trained["execution_provenance"]["best_checkpoint"]["source"] == "current_artifact"
        assert trained["execution_provenance"]["feature_transform"] == trained.get("feature_transform")
        assert trained["execution_provenance"]["training_updates"] == trained["history"][-1]["updates"]
        assert trained["best_source_content_id"] is None
        backtest = service.backtest_model(model["id"], {"dataset_id": request["dataset_id"], "version_id": train_id,
            "spec": {"start": request["spec"]["validation_start"], "end": request["spec"]["validation_end"],
                     "baseline": "policy", "evaluation_purpose": "development_replay"}})
        backtest_id = backtest["id"]
        frozen_backtest = json.loads(Path(app.state.service.store.get_job(backtest_id).input_path).read_bytes())
        assert frozen_backtest["input"]["policy_context"] == version_context
        finished(backtest_id)
        summary_root = app.state.service.store.artifact_root / backtest_id / "final" / "artifact"
        summary = json.loads((summary_root / "summary.json").read_bytes())
        assert summary["framework"] == frozen_backtest["input"]["resolved_configuration"]
        assert summary["execution_provenance"]["policy_version_id"] == train_id
        assert summary["execution_provenance"]["best_checkpoint"]["source_content_id"] == trained["id"]
        assert summary["execution_provenance"]["last_checkpoint"]["cumulative_timesteps"] == trained["timesteps_completed"]
        assert summary["execution_provenance"]["feature_transform"] == trained.get("feature_transform")
        with (summary_root / "positions.csv").open(encoding="utf-8", newline="") as stream:
            assert all(row["instrument_id"] == "SZSE:etf:000001" for row in csv.DictReader(stream))
        assert read_manifest(policy_root, "policy")["id"] == trained["id"]


def test_canonical_rows_distinguish_same_code_across_exchanges_and_reject_unknown(tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    path = root / "positions.csv"
    path.write_text("date,symbol,shares\n2024-01-01,A,1\n2024-01-01,B,2\n", encoding="utf-8")
    resolved = SimpleNamespace(ordered_pairs=(
        SimpleNamespace(local_symbol="A", instrument_id=InstrumentId(exchange="SZSE", instrument_type="etf", code="000001")),
        SimpleNamespace(local_symbol="B", instrument_id=InstrumentId(exchange="SSE", instrument_type="etf", code="000001")),
    ))
    _canonical_csv_rows(root, resolved)
    with path.open(encoding="utf-8", newline="") as stream:
        assert [(row["symbol"], row["instrument_id"]) for row in csv.DictReader(stream)] == [
            ("A", "SZSE:etf:000001"), ("B", "SSE:etf:000001")]
    path.write_text("date,symbol,shares\n2024-01-01,UNKNOWN,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unbound symbol UNKNOWN"):
        _canonical_csv_rows(root, resolved)


def test_training_best_ancestor_is_distinct_from_last_checkpoint():
    resolved = SimpleNamespace(internal_training_spec={"train_start": "2024-01-01", "validation_end": "2024-02-01"},
                               basket=SimpleNamespace(selection_note="hand selected"))
    payload = {"execution_environment": {"python": "3.12"}, "request": {"model_id": "model-1"}}
    saved = {"id": "new-version", "best_source_content_id": "earlier-version", "best_timesteps": 8,
             "timesteps_completed": 24, "session_timesteps": 8, "files": {"last.zip": "digest"}}
    provenance = _execution_provenance(resolved, payload, {"source.py": "digest"}, policy_manifest=saved)
    assert provenance["best_checkpoint"] == {"source": "ancestor", "source_content_id": "earlier-version", "timesteps": 8}
    assert provenance["last_checkpoint"] == {"source": "current_artifact", "source_content_id": None,
                                             "cumulative_timesteps": 24, "session_timesteps": 8}


def test_selected_only_policy_does_not_claim_a_last_checkpoint():
    resolved = SimpleNamespace(internal_training_spec={"train_start": "2019-01-01"},
                               basket=SimpleNamespace(selection_note="hand selected"))
    payload = {"execution_environment": {"python": "3.12"}, "request": {"model_id": "model-1"}}
    saved = {"id": "selected-version", "best_timesteps": 64, "timesteps_completed": 128,
             "files": {"policy.zip": "digest"}, "history": [{"updates": 8}]}
    provenance = _execution_provenance(resolved, payload, {"source.py": "digest"},
                                       policy_version_id="selected-version", policy_manifest=saved)
    assert provenance["best_checkpoint"] == {"source": "current_artifact",
                                             "source_content_id": "selected-version", "timesteps": 64}
    assert provenance["last_checkpoint"] is None
