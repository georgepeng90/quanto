"""Model publication and queueing revalidate immutable inputs at commit time."""
import json

import pytest

from quanto.investing.models import InvestingModels
from quanto.workbench.configuration import FrameworkConflict, FrameworkResolver
from tests.integration.test_investing_models import model_workspace


def _change_dataset(store, identity):
    with store.transaction() as db:
        db.execute("UPDATE datasets SET content_hash=? WHERE id=?", ("0" * 64, identity))


def test_model_creation_rechecks_dataset_before_publishing(model_workspace, monkeypatch):
    app, _, _, request = model_workspace
    store = app.state.service.store
    original = FrameworkResolver.resolve

    def change_after_preflight(self, operation, *, db=None):
        resolved = original(self, operation, db=db)
        if db is None:
            _change_dataset(store, request["dataset_id"])
        return resolved

    monkeypatch.setattr(FrameworkResolver, "resolve", change_after_preflight)
    with pytest.raises(FrameworkConflict) as caught:
        InvestingModels(app.state.service).create_model(request)
    assert caught.value.report.status == "blocked"
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM investing_models").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM model_framework_bindings").fetchone()[0] == 0


def test_training_rechecks_inputs_and_discards_only_its_unregistered_snapshot(model_workspace, monkeypatch):
    from quanto.workbench import snapshots

    app, _, _, request = model_workspace
    service = InvestingModels(app.state.service)
    model = service.create_model(request)
    historical = service.store.root / "job-inputs" / "preserved-history"
    historical.mkdir(parents=True)
    (historical / "input.json").write_text("historical", encoding="utf-8")
    original = snapshots._execution_files

    def change_during_freeze(root):
        files = original(root)
        _change_dataset(service.store, request["dataset_id"])
        return files

    monkeypatch.setattr(snapshots, "_execution_files", change_during_freeze)
    with pytest.raises(FrameworkConflict) as caught:
        service.train_model(model["id"], {"additional_timesteps": 8})
    assert caught.value.report.status == "blocked"
    assert service.store.list_jobs() == []
    assert list(historical.parent.iterdir()) == [historical]
    assert (historical / "input.json").read_text(encoding="utf-8") == "historical"
    with service.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM investing_sessions").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM model_framework_bindings").fetchone()[0] == 1


@pytest.mark.parametrize("target", ["source", "input"])
def test_learning_snapshot_tamper_before_registration_never_queues(model_workspace, monkeypatch, target):
    from quanto.learning import service as learning_service

    app, _, _, request = model_workspace
    service = InvestingModels(app.state.service)
    model = service.create_model(request)
    original = learning_service.atomic_json

    def corrupt_written_input(path, value):
        original(path, value)
        if target == "source":
            (path.parent / "src" / "quanto" / "__init__.py").write_text("changed", encoding="utf-8")
        else:
            changed = json.loads(path.read_bytes())
            changed["input"]["request"]["spec"]["name"] = "changed after freeze"
            path.write_text(json.dumps(changed), encoding="utf-8")

    monkeypatch.setattr(learning_service, "atomic_json", corrupt_written_input)
    with pytest.raises(FrameworkConflict):
        service.train_model(model["id"], {"additional_timesteps": 8})
    assert service.store.list_jobs() == []
    assert not list((service.store.root / "job-inputs").iterdir())
