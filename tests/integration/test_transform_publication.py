"""A fitted transform becomes usable only with its successful policy version."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

from quanto.factors.features import FrozenTransform
from quanto.framework import DefinitionRef
from quanto.investing.models import InvestingModels
from quanto.learning.artifacts import read_frozen_transform, read_manifest, write_frozen_transform, write_manifest
from quanto.learning.registry import finish_learning_job, hash_files
from quanto.workbench.store import WorkbenchStore


def fitted_transform():
    return FrozenTransform(
        DefinitionRef(kind="feature", id="fit-example", version="1"), "a" * 64, "b" * 64,
        "standardize-v1", date(2024, 1, 2), date(2024, 1, 3),
        datetime(2024, 1, 3, 7, tzinfo=timezone.utc), ("c" * 64,),
        ("d" * 64,), ("e" * 64,), {"mean": 2.5, "scale": 1.25},
    )


def staged_policy(tmp_path, *, state="running"):
    store = WorkbenchStore(tmp_path / "workspace")
    with store.transaction() as db:
        db.execute("INSERT INTO jobs(id,kind,state,enqueue_seq,artifact_root) VALUES ('session','train_policy',?,1,?)",
                   (state, str(tmp_path)))
        db.execute("INSERT INTO investing_sessions VALUES ('session','model',NULL)")
    root = tmp_path / "final" / "artifact"
    root.mkdir(parents=True)
    descriptor = write_frozen_transform(root, fitted_transform())
    write_manifest(root, {"kind": "policy", "feature_transform": descriptor})
    hashes = {f"artifact/{name}": value for name, value in hash_files(root).items()}
    return store, root, descriptor, {"artifact_kind": "policy", "hashes": hashes}


def test_frozen_transform_roundtrips_with_semantic_and_file_identity(tmp_path):
    root = tmp_path / "policy"
    root.mkdir()
    original = fitted_transform()
    descriptor = write_frozen_transform(root, original)
    manifest = write_manifest(root, {"kind": "policy", "feature_transform": descriptor})
    assert descriptor == {"path": "transform.json", "transform_hash": original.transform_hash}
    assert "transform.json" in manifest["files"]
    assert read_frozen_transform(root, descriptor) == original

    changed = json.loads((root / "transform.json").read_text(encoding="utf-8"))
    changed["data"]["mean"] = 9
    (root / "transform.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="transform"):
        read_frozen_transform(root, descriptor)
    with pytest.raises(ValueError, match="integrity|hash"):
        read_manifest(root, "policy")


def test_transform_reader_rejects_missing_required_identity_field(tmp_path):
    root = tmp_path / "policy"
    root.mkdir()
    descriptor = write_frozen_transform(root, fitted_transform())
    payload = json.loads((root / "transform.json").read_text(encoding="utf-8"))
    del payload["training_binding_hashes"]
    (root / "transform.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="transform"):
        read_frozen_transform(root, descriptor)


def test_policy_and_transform_publish_in_one_success_transaction(tmp_path):
    store, root, descriptor, result = staged_policy(tmp_path)
    assert finish_learning_job(store, "session", root.parent, result)
    with store.transaction() as db:
        assert db.execute("SELECT state FROM jobs WHERE id='session'").fetchone()[0] == "succeeded"
        assert InvestingModels._head(db, "model") == "session"
        row = db.execute("SELECT manifest_json,files_json FROM learning_artifacts WHERE id='session'").fetchone()
    assert json.loads(row[0])["feature_transform"] == descriptor
    assert "transform.json" in json.loads(row[1])
    assert read_frozen_transform(root, descriptor) == fitted_transform()


@pytest.mark.parametrize("state", ["cancel_requested", "failed"])
def test_unsuccessful_session_cannot_publish_staged_transform(tmp_path, state):
    store, root, _, result = staged_policy(tmp_path, state=state)
    assert not finish_learning_job(store, "session", root.parent, result)
    with store.transaction() as db:
        assert InvestingModels._head(db, "model") is None
        assert db.execute("SELECT count(*) FROM learning_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
def test_db_publication_failure_rolls_back_artifact_and_success_state(tmp_path, operation):
    store, root, _, result = staged_policy(tmp_path)
    table = "learning_artifacts" if operation == "INSERT" else "jobs"
    with store.transaction() as db:
        db.execute(f"CREATE TRIGGER fail_publication BEFORE {operation} ON {table} "
                   "BEGIN SELECT RAISE(ABORT, 'publication failed'); END")
    with pytest.raises(Exception, match="publication failed"):
        finish_learning_job(store, "session", root.parent, result)
    with store.transaction() as db:
        assert db.execute("SELECT state FROM jobs WHERE id='session'").fetchone()[0] == "running"
        assert InvestingModels._head(db, "model") is None
        assert db.execute("SELECT count(*) FROM learning_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("orphan", [True, False])
def test_publication_rejects_unpaired_transform_file_and_descriptor(tmp_path, orphan):
    store, root, _, result = staged_policy(tmp_path)
    if orphan:
        (root / "manifest.json").unlink()
        write_manifest(root, {"kind": "policy"})
    else:
        (root / "transform.json").unlink()
    result["hashes"] = {f"artifact/{name}": value for name, value in hash_files(root).items()}
    with pytest.raises(ValueError, match="transform"):
        finish_learning_job(store, "session", root.parent, result)
    with store.transaction() as db:
        assert InvestingModels._head(db, "model") is None
