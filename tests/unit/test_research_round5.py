"""Round 5 selection uses frozen early evidence and distinct equity paths."""

import pytest

from scripts import research_round5 as research


def row(name, period, annual, curve_hash, sharpe=1.0):
    return {"candidate": name, "period": period,
            "metrics": {"annual_return": annual, "max_drawdown": 0.0, "sharpe": sharpe},
            "artifacts": {"curves.csv": curve_hash}}


def test_selection_ranks_period_improvement_and_ignores_late_winner():
    rows = [row("baseline", "development", .10, "bd"),
            row("baseline", "validation", .20, "bv"),
            row("steady", "development", .12, "sd"),
            row("steady", "validation", .21, "sv"),
            row("tempting", "development", .15, "td"),
            row("tempting", "validation", .19, "tv"),
            row("tempting", "late", 99, "late"),
            row("tempting", "full", 99, "full")]
    selection = research.choose(rows, ["baseline", "steady", "tempting"])
    assert selection["selected"] == ["steady", "tempting"]
    assert selection["groups"][0]["minimum_improvement"] == pytest.approx(.01)
    assert selection["groups"][1]["minimum_improvement"] == pytest.approx(-.01)


def test_selection_deduplicates_ordered_hash_pairs_and_excludes_baseline_alias():
    rows = []
    for name, development, validation, hashes in (
            ("baseline", .10, .10, ("bd", "bv")),
            ("baseline_alias", .90, .90, ("bd", "bv")),
            ("first", .15, .15, ("same_d", "same_v")),
            ("second", .13, .13, ("same_d", "same_v")),
            ("reverse", .12, .12, ("same_v", "same_d"))):
        rows.extend([row(name, "development", development, hashes[0]),
                     row(name, "validation", validation, hashes[1])])
    selection = research.choose(rows, ["baseline", "baseline_alias", "first", "second", "reverse"])
    assert selection["baseline_aliases"] == ["baseline", "baseline_alias"]
    assert selection["selected"] == ["first", "reverse"]
    assert selection["groups"][0]["aliases"] == ["first", "second"]
    assert selection["groups"][0]["curve_hashes"] == ["same_d", "same_v"]
    assert selection["groups"][1]["curve_hashes"] == ["same_v", "same_d"]


def test_selection_can_choose_fewer_than_three_and_requires_early_evidence():
    rows = [row("baseline", "development", .10, "bd"),
            row("baseline", "validation", .10, "bv"),
            row("only", "development", .11, "od"),
            row("only", "validation", .11, "ov")]
    assert research.choose(rows, ["baseline", "only"])["selected"] == ["only"]
    with pytest.raises(ValueError, match="Missing result"):
        research.choose(rows[:-1], ["baseline", "only"])
