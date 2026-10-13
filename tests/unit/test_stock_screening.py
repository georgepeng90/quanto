from copy import deepcopy

import pytest

from quanto.screening.contracts import ScreeningRequest
from quanto.screening.engine import screen
from tests.fixtures.screening_cases import screening_request


def run(payload):
    return screen(ScreeningRequest.model_validate(payload))


def test_deterministic_limited_evidence_and_shortfall():
    payload = screening_request()
    result = run(payload)
    assert result == run(payload)
    assert len(result.members) == 1 and result.shortfall == 1
    assert not result.historical_selection_validation
    assert result.evidence_assurance == "agent_supplied_citations_not_independently_verified"
    assert result.evidence[0].archive_level == "excerpt"


@pytest.mark.parametrize("change,reason", [
    (lambda p: p["rules"].update(min_average_amount=10001), "liquidity_below_minimum"),
    (lambda p: p["candidates"][0].update(industry=None), "missing_industry"),
    (lambda p: p["candidates"][0].update(status=None), "missing_status"),
    (lambda p: p["candidates"][0].update(status_date="2026-10-12"), "stale_status"),
    (lambda p: p["candidates"][0].update(risks=[]), "incomplete_research"),
    (lambda p: p["candidates"][0]["citations"].pop("listing_date"), "missing_citation:listing_date"),
    (lambda p: p["evidence"][0].update(published_at="2026-10-14T08:00:00+08:00"), "unavailable_evidence:source"),
    (lambda p: p["evidence"][0].update(published_at="2026-10-13"), "unavailable_evidence:source"),
    (lambda p: p.update(trading_days=[]), "missing_calendar"),
])
def test_hard_rules_cannot_be_overridden(change, reason):
    payload = screening_request()
    change(payload)
    result = run(payload)
    assert not result.members and result.shortfall == 2
    assert reason in result.candidates[0].reasons


def test_industry_cap_uses_agent_order_and_retains_every_candidate():
    payload = screening_request()
    second = deepcopy(payload["candidates"][0])
    second["instrument_id"]["code"] = "000002"
    second["rank"] = 0
    payload["evidence"][0]["instruments"].append(second["instrument_id"])
    payload["candidates"].append(second)
    result = run(payload)
    assert result.members[0].code == "000002"
    assert result.candidates[0].reasons == ("industry_limit",)
    payload["candidates"][0]["recommendation"] = "not_researched"
    assert run(payload).candidates[0].decision == "not_researched"


@pytest.mark.parametrize("mutation", [
    lambda p: p["candidates"].append(deepcopy(p["candidates"][0])),
    lambda p: p["candidates"][0]["citations"]["board"][0].update(quote="fabricated quote"),
    lambda p: p["candidates"][0]["citations"]["board"][0].update(evidence_id="missing"),
    lambda p: p["evidence"][0].update(instruments=[]),
    lambda p: p["rules"].update(min_average_amount=float("nan")),
    lambda p: p["rules"].update(target_count=True),
    lambda p: p.update(as_of="2026-10-13T16:00:00"),
])
def test_invalid_or_fabricated_input_is_rejected(mutation):
    payload = screening_request()
    mutation(payload)
    with pytest.raises(ValueError):
        run(payload)


def test_identity_not_bare_code_and_future_unused_material():
    payload = screening_request()
    other = deepcopy(payload["candidates"][0])
    other["instrument_id"]["exchange"] = "OTHER"
    payload["evidence"][0]["instruments"].append(other["instrument_id"])
    payload["candidates"].append(other)
    result = run(payload)
    assert len(result.members) == 1
    assert "unsupported_instrument" in result.candidates[1].reasons
    extra = deepcopy(payload["evidence"][0])
    extra.update(id="future", published_at="2026-10-15T08:00:00+08:00")
    payload["evidence"].append(extra)
    assert run(payload).members == result.members


def test_date_only_uses_next_session_close_and_revisions_block_stale_facts():
    payload = screening_request()
    payload["evidence"][0]["published_at"] = "2026-10-12"
    assert run(payload).members
    payload["as_of"] = "2026-10-13T14:00:00+08:00"
    assert not run(payload).members
    payload = screening_request()
    revision = deepcopy(payload["evidence"][0])
    revision.update(id="revised", revises="source")
    payload["evidence"].append(revision)
    assert "superseded_evidence:source" in run(payload).candidates[0].reasons


def test_future_bars_do_not_supply_liquidity_or_history():
    payload = screening_request()
    payload["candidates"][0]["daily"][1]["date"] = "2026-10-14"
    result = run(payload)
    assert not result.members
    assert "insufficient_history" in result.candidates[0].reasons
