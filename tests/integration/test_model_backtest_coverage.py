"""A saved policy can use later data without its training history."""

import json

import pandas as pd

from quanto.investing.evaluation import policy_identity
from quanto.learning.artifacts import write_manifest
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.workbench.bindings import BindingStore
from quanto.workbench.etf_model_migration import ModelMigrator
from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.framework_requests import PolicyBacktestRequest
from quanto.workbench.store import utc_now
from tests.fixtures.framework_imports import fixture_identity_evidence
from tests.fixtures.frozen_policy_source import attach_frozen_policy_input
from tests.fixtures.investing_training_cases import investing_case
from tests.integration.test_framework_capabilities import _training_case
from tests.integration.test_model_framework_bindings import _old_model


def test_policy_backtest_uses_new_dataset_without_training_dates_or_extra_member(tmp_path):
    store, create, _ = _training_case(tmp_path)
    original_dataset_id = create.dataset_id
    original_spec = _old_model(store, create)
    original_binding = BindingStore(store).get_asset_binding(original_dataset_id)

    artifact = tmp_path / "policy"
    artifact.mkdir()
    (artifact / "policy.zip").write_bytes(b"selected-weights")
    (artifact / "last.zip").write_bytes(b"optimizer-state")
    manifest = write_manifest(artifact, {
        "kind": "policy", "version": 1, "config": original_spec,
        "model_id": "old-model", "dataset_content_hash": original_binding.dataset_content_hash,
        "basket": original_spec["backtest"]["symbols"], "price_mode": "raw_cash",
        "parent_content_id": None, "best_source_content_id": None,
        "cutoff": original_spec["validation_end"],
        "selection_metric": original_spec["selection_metric"],
        **policy_identity(create.spec.to_spec(original_binding.resolve(
            tuple(asset.instrument_id for asset in original_binding.assets)))),
    })
    files = hash_files(artifact)
    with store.transaction() as db:
        db.execute("""INSERT INTO jobs (id,kind,state,enqueue_seq,artifact_root,created_at)
                      VALUES (?,'train_policy','succeeded',1,?,?)""",
                   ("version-one", str(tmp_path), utc_now()))
        db.execute("INSERT INTO investing_sessions VALUES (?,?,?)", ("version-one", "old-model", None))
        db.execute("INSERT INTO learning_artifacts VALUES (?,?,?,?,?,?,?)",
                   ("version-one", "policy", str(artifact), digest(json_bytes(files)),
                    json_bytes(manifest).decode(), json_bytes(files).decode(), utc_now()))
    attach_frozen_policy_input(store, tmp_path, "version-one", original_dataset_id, original_spec, "old-model")
    model_binding = ModelMigrator(store).migrate_model("old-model")

    source = tmp_path / "later-source"
    _, dates = investing_case(source)
    first = dates[80]
    for name in ("prices", "status", "benchmark", "calendar"):
        path = source / f"{name}.csv"
        frame = pd.read_csv(path, dtype={"symbol": str})
        frame = frame.loc[pd.to_datetime(frame["date"]) >= first]
        if name in ("prices", "status"):
            frame = pd.concat([frame, frame.assign(symbol="000002")], ignore_index=True)
        frame.to_csv(path, index=False)
    instruments = source / "instruments.csv"
    frame = pd.read_csv(instruments, dtype={"symbol": str})
    pd.concat([frame, frame.assign(symbol="000002")], ignore_index=True).to_csv(instruments, index=False)
    dataset_manifest = source / "manifest.json"
    values = json.loads(dataset_manifest.read_text())
    values.update(calendar_start=str(first.date()), data_start=str(first.date()))
    dataset_manifest.write_text(json.dumps(values))
    later = store.register_dataset(source, identity_evidence=fixture_identity_evidence(source))

    assert later.id != original_dataset_id
    assert first.date() > create.spec.train_end
    assert dates[90].date() > first.date()
    assert len(BindingStore(store).get_asset_binding(later.id).assets) == 2
    request = PolicyBacktestRequest(version_id="version-one", dataset_id=later.id,
                                    spec={"start": str(dates[90].date()),
                                          "end": str(dates[99].date()),
                                          "evaluation_purpose": "post_selection_backtest"})
    resolved = FrameworkResolver(store).resolve(request)

    assert resolved.capability_report.status == "ready"
    assert resolved.capability_report.dataset_version_id == later.id
    assert tuple(resolved.internal_backtest_spec["symbols"]) == tuple(original_spec["backtest"]["symbols"])
    assert tuple(pair.instrument_id for pair in resolved.ordered_pairs) == model_binding.basket.members
    assert model_binding.dataset_version_id == original_dataset_id
    assert model_binding.training_configuration.asset_binding.dataset_version_id == original_dataset_id
