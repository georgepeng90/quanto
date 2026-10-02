from pathlib import Path

import pytest

from scripts.research_round3 import choose_training, choose_shortlist, load_cached, write_json, digest


def row(name, period, annual, drawdown=.1):
    return {"candidate": name, "period": period, "metrics": {
        "annual_return": annual, "max_drawdown": drawdown, "sharpe": annual * 5}}


def test_training_selection_ignores_later_winner_and_uses_drawdown():
    families = {"a": ["a1", "a2"], "b": ["b1", "b2"]}
    rows = [row("a1", "development", .1, .1), row("a2", "development", .12, .2),
            row("b1", "development", .03), row("b2", "development", .04),
            row("a2", "late", 100)]
    assert choose_training(rows, families) == {"a": "a1", "b": "b2"}


def test_shortlist_uses_worst_training_validation_score_not_unselected_or_late():
    selected = {"a": "a1", "b": "b1", "c": "c1", "d": "d1"}
    rows = [row(name, period, value) for name, value in
            [("a1", .05), ("b1", .1), ("c1", .15), ("d1", .2)]
            for period in ("development", "validation")]
    rows += [row("a1", "late", 99), row("a2", "validation", 99)]
    rows[-3] = row("d1", "validation", -.2)
    assert choose_shortlist(rows, selected) == ["c1", "b1", "a1"]


def test_selection_rejects_incomplete_and_duplicate_evidence():
    with pytest.raises(ValueError, match="Missing"):
        choose_training([row("a1", "development", .1)], {"a": ["a1", "a2"]})
    with pytest.raises(ValueError, match="Duplicate"):
        choose_training([row("a1", "development", .1)] * 2, {"a": ["a1"]})


def test_resume_rejects_wrong_identity_and_changed_artifacts(tmp_path: Path):
    expected = {"candidate": "a", "period": "late", "cost_multiplier": 1,
                "protocol_sha256": "frozen"}
    (tmp_path / "curves.csv").write_text("date,equity\n2026-01-05,100\n")
    saved = expected | {"artifacts": {"curves.csv": digest(tmp_path / "curves.csv")}}
    write_json(tmp_path / "summary.json", saved)
    assert load_cached(tmp_path, expected) == saved
    with pytest.raises(ValueError, match="identity"):
        load_cached(tmp_path, expected | {"cost_multiplier": 2})
    (tmp_path / "curves.csv").write_text("altered")
    with pytest.raises(ValueError, match="artifact"):
        load_cached(tmp_path, expected)
