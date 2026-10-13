"""Protect existing ETF behavior while the shared framework is introduced."""
import json
from copy import deepcopy

import numpy as np
import pytest

from tests.fixtures.etf_framework_baseline import (
    REFERENCE_PATH, capture_etf_reference, case_definitions, write_reference,
)


@pytest.fixture(scope="module")
def captured():
    return capture_etf_reference()


@pytest.fixture(scope="module")
def reference():
    return json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))


def assert_equivalent(actual, expected, path="root"):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), path
        for key in expected:
            assert_equivalent(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for index, value in enumerate(expected):
            assert_equivalent(actual[index], value, f"{path}[{index}]")
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12), path
    else:
        assert actual == expected, path


@pytest.mark.parametrize("case_id", [case["id"] for case in case_definitions()])
def test_etf_observations_actions_rewards_and_account_match_pre_framework_reference(captured, reference, case_id):
    actual, expected = captured["cases"][case_id], reference["cases"][case_id]
    np.testing.assert_array_equal(actual["initial"]["observation"], expected["initial"]["observation"])
    for step, old_step in zip(actual["trace"], expected["trace"], strict=True):
        np.testing.assert_array_equal(step["observation"], old_step["observation"])
    # New explicit fee columns are zero for ETFs; all original economic results remain frozen.
    actual = deepcopy(actual)
    fills = actual["tables"]["fills"]
    for name in ("stamp_duty", "transfer_fee"):
        index = fills["columns"].index(name)
        assert all(row[index] == 0 for row in fills["rows"])
        fills["columns"].pop(index)
        for row in fills["rows"]:
            row.pop(index)
    assert_equivalent(actual, expected, case_id)


def test_reference_replays_the_frozen_inputs_and_keeps_source_evidence(captured, reference):
    assert captured["inputs"] == reference["inputs"]
    assert len(reference["evidence"]["source_commit"]) == 40
    assert "src/quanto/investing/environment.py" in reference["evidence"]["source_hashes"]
    assert reference["evidence"]["dependencies"]["stable-baselines3"] == "2.9.0"


def _rows(case, table):
    data = case["tables"][table]
    return [dict(zip(data["columns"], row, strict=True)) for row in data["rows"]]


def _weekly(captured, reward="net-log-return-v1"):
    return captured["cases"][f"weekly-hold-target-cash-quartiles-v2-{reward}"]


def test_reference_exercises_targets_hold_cash_and_next_open(captured):
    case = _weekly(captured)
    signals = {row["date"][:10]: row for row in _rows(case, "signals")}
    assert signals["2024-01-05"]["weights"] == {"510300": .5, "159915": .5}
    assert signals["2024-01-12"]["intent"] == "hold"
    assert signals["2024-01-19"]["weights"] == {}
    fills = _rows(case, "fills")
    assert fills[0]["trade_date"][:10] == "2024-01-08"
    assert {row["side"] for row in fills} == {"buy", "sell"}
    assert all(row["commission"] > 0 for row in fills)
    assert any(row["shares"] > 0 for row in _rows(case, "positions") if row["date"][:10] == "2024-02-05")


def test_reference_exercises_month_end_and_daily_risk_between_decisions(captured):
    monthly = captured["cases"]["monthly-legacy"]
    assert monthly["initial"]["info"]["asof"] == "2024-01-31"
    assert [step["info"]["asof"] for step in monthly["trace"]] == ["2024-02-29", "2024-03-01"]
    assert _rows(monthly, "fills")[0]["trade_date"][:10] == "2024-02-01"
    risk = captured["cases"]["daily-risk"]
    events = _rows(risk, "risk_events")
    assert any(row["code"] == "DRAWDOWN_PAUSE" and row["date"][:10] == "2024-01-09" for row in events)
    assert any(row["side"] == "sell" and row["trade_date"][:10] == "2024-01-10" for row in _rows(risk, "fills"))
    signal = next(row for row in _rows(risk, "signals") if row["date"][:10] == "2024-01-09")
    assert signal["intent"] == "hold" and signal["approved_intent"] == "target"


def test_reference_exercises_cash_events_and_dsr_state_without_duplicate_accounts(captured):
    # Matching synthetic quotes prove event replay semantics, not real adjusted-return equivalence.
    raw, adjusted = _weekly(captured), captured["cases"]["weekly-adjusted"]
    raw_events = _rows(raw, "account_events")
    assert any(row["event_type"] == "dividend_receivable" and row["amount"] > 0 for row in raw_events)
    assert any(row["event_type"] == "dividend_payment" and row["amount"] > 0 for row in raw_events)
    assert not any(row["event_type"].startswith("dividend") for row in _rows(adjusted, "account_events"))
    dsr, state_log = _weekly(captured, "differential-sharpe-v1"), _weekly(captured, "net-log-return-with-state-v1")
    assert len(dsr["initial"]["observation"]) == 30
    assert len(raw["initial"]["observation"]) == 28
    assert dsr["initial"]["info"]["reward_daily_count"] == 5
    assert dsr["initial"]["observation"][-2] != 0
    assert dsr["trace"][0]["reward"] != state_log["trace"][0]["reward"]
    assert dsr["tables"] == state_log["tables"] == raw["tables"]


def test_reference_generator_refuses_to_replace_frozen_evidence(tmp_path):
    target = tmp_path / "existing.json"
    target.write_text("keep original", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        write_reference(target)
    assert target.read_text(encoding="utf-8") == "keep original"
