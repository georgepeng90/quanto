from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from quanto.framework import FrozenBasketRevision, ModelFrameworkBinding, ResolvedConfiguration, canonical_hash
from quanto.screening.contracts import ScreeningRequest
from quanto.screening.store import ScreeningStore
from quanto.workbench.baskets import BasketStore
from quanto.workbench.frozen_configuration import verify_frozen_configuration
from quanto.workbench.store import WorkbenchStore
from tests.fixtures.screening_cases import screening_request
from tests.integration.test_stock_training import stock_workspace


def test_atomic_handoff_idempotency_and_zero_result(tmp_path):
    store = WorkbenchStore(tmp_path)
    screenings = ScreeningStore(store)
    payload = screening_request()
    record = screenings.submit(ScreeningRequest.model_validate(payload))
    with ThreadPoolExecutor(max_workers=3) as pool:
        baskets = list(pool.map(lambda _: screenings.create_basket(record.id, "Research basket", "use"), range(3)))
    assert baskets[0] == baskets[1] == baskets[2]
    basket = baskets[0]
    assert basket.members == record.result.members
    assert basket.selection_origin.result_hash == record.result_hash
    assert screenings.validate_basket(basket) == record
    with pytest.raises(ValueError, match="conflict"):
        screenings.create_basket(record.id, "Other name", "use")
    changed = basket.model_dump(mode="json", exclude={"content_hash"})
    changed["members"][0]["code"] = "000002"
    with pytest.raises(ValueError, match="members"):
        screenings.validate_basket(FrozenBasketRevision.model_validate(changed))
    payload["rules"]["min_average_amount"] = 1e10
    payload["idempotency_key"] = "empty"
    empty = screenings.submit(ScreeningRequest.model_validate(payload))
    with pytest.raises(ValueError, match="no selected"):
        screenings.create_basket(empty.id, "Empty", "empty")
    assert store.list_jobs() == []


def test_absent_origin_preserves_old_hash_and_revisions_drop_origin(tmp_path):
    store = WorkbenchStore(tmp_path)
    screenings = ScreeningStore(store)
    record = screenings.submit(ScreeningRequest.model_validate(screening_request()))
    basket = screenings.create_basket(record.id, "Basket", "use")
    revised = BasketStore(store).revise(basket.basket_id, 1, basket.members, "manual research")
    assert "selection_origin" not in revised.model_dump(mode="json")
    assert revised.content_hash == canonical_hash(revised.model_dump(mode="json", exclude={"content_hash", "selection_origin"}))


def test_basket_failure_rolls_back_all_indices(tmp_path, monkeypatch):
    store = WorkbenchStore(tmp_path)
    screenings = ScreeningStore(store)
    record = screenings.submit(ScreeningRequest.model_validate(screening_request()))
    insert = BasketStore._insert
    def fail(db, revision):
        insert(db, revision)
        raise RuntimeError("injected failure")
    with monkeypatch.context() as patch:
        patch.setattr(BasketStore, "_insert", staticmethod(fail))
        with pytest.raises(RuntimeError):
            screenings.create_basket(record.id, "Basket", "use")
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM baskets").fetchone()[0] == 0
    assert screenings.create_basket(record.id, "Basket", "use").members


def test_model_job_freezes_research_and_offline_validation_needs_no_database(tmp_path, monkeypatch):
    app, client, headers, request = stock_workspace(tmp_path, monkeypatch)
    store = app.state.service.store
    response = client.post("/api/screenings", headers=headers, json=screening_request())
    assert response.status_code == 200, response.text
    record = response.json()
    response = client.post(f'/api/screenings/{record["id"]}/basket', headers=headers,
                           json={"name": "Screened stocks", "idempotency_key": "use"})
    assert response.status_code == 200, response.text
    basket = response.json()
    request["basket_revision_ref"] = {"basket_id": basket["basket_id"], "revision": 1}
    created = client.post("/api/investing/models", headers=headers, json=request)
    assert created.status_code == 200, created.text
    assert store.list_jobs() == []
    response = client.post(f'/api/investing/models/{created.json()["id"]}/train', headers=headers,
                           json={"additional_timesteps": 4, "device": "cpu"})
    assert response.status_code == 200, response.text
    job = store.get_job(response.json()["id"])
    payload = json.loads(Path(job.input_path).read_text(encoding="utf-8"))["input"]
    assert payload["screening_record"] == record
    monkeypatch.setattr(ScreeningStore, "get", lambda *a, **k: pytest.fail("offline consulted live database"))
    resolved = ResolvedConfiguration.model_validate(payload["resolved_configuration"])
    binding = ModelFrameworkBinding.model_validate(payload["model_binding"])
    verify_frozen_configuration(resolved, dataset_ref=payload["dataset_ref"], model_binding=binding,
                                screening_record=payload["screening_record"])
    with pytest.raises(ValueError, match="screening"):
        verify_frozen_configuration(resolved, dataset_ref=payload["dataset_ref"], model_binding=binding)
    payload["screening_record"]["request"]["evidence"][0]["text"] = "tampered"
    with pytest.raises(ValueError):
        verify_frozen_configuration(resolved, dataset_ref=payload["dataset_ref"], model_binding=binding,
                                    screening_record=payload["screening_record"])
