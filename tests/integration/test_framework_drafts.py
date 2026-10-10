import pytest

from quanto.workbench.configuration import FrameworkResolver
from quanto.workbench.framework_requests import FixedRequest
from tests.integration.test_framework_capabilities import _fixed_case


def test_drafts_store_one_public_parameter_source(tmp_path):
    store, request = _fixed_case(tmp_path)
    draft = store.save_experiment(None, None, request.model_dump(mode="json"))
    assert draft.payload == request.model_dump(mode="json")
    assert "effective_strategy_params" not in draft.payload
    assert "symbols" not in draft.payload["backtest_spec"]


def test_drafts_reject_the_previous_free_symbol_contract(tmp_path):
    store, request = _fixed_case(tmp_path)
    values = request.model_dump(mode="json")
    values["backtest_spec"]["symbols"] = ["SZSE:etf:000001"]
    with store.transaction() as db:
        before = db.execute("SELECT count(*) FROM experiments").fetchone()[0]
    with pytest.raises(ValueError):
        store.save_experiment(None, None, values)
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM experiments").fetchone()[0] == before


def test_unready_well_formed_drafts_can_be_saved_without_submission(tmp_path):
    store, request = _fixed_case(tmp_path)
    values = request.model_dump(mode="json")
    values["strategy_revision_id"] = "not-registered-yet"
    draft = store.save_experiment(None, None, values)
    assert draft.payload["strategy_revision_id"] == "not-registered-yet"
    assert FrameworkResolver(store).check(FixedRequest.model_validate(draft.payload)).status == "blocked"
    assert store.list_jobs() == []
