import pytest

from scripts.research_round4 import choose


def row(name, period, annual, drawdown, sharpe=1.0):
    return {"candidate": name, "period": period,
            "metrics": {"annual_return": annual, "max_drawdown": drawdown, "sharpe": sharpe}}


def test_selection_requires_balanced_early_periods_and_ignores_later_winner():
    rows = [row("spiky", "development", .1, .1), row("spiky", "validation", .8, .4),
            row("balanced", "development", .12, .1), row("balanced", "validation", .12, .1),
            row("spiky", "late", 10, 0), row("spiky", "full", 10, 0)]
    assert choose(rows, {"family": ["spiky", "balanced"]}) == {"family": "balanced"}


def test_selection_breaks_score_ties_with_weaker_period_sharpe():
    rows = [row("A", "development", .1, .1, .2), row("A", "validation", .1, .1, 2),
            row("B", "development", .1, .1, .4), row("B", "validation", .1, .1, .4)]
    assert choose(rows, {"family": ["A", "B"]}) == {"family": "B"}


def test_selection_cannot_use_missing_early_period():
    with pytest.raises(ValueError, match="Missing result"):
        choose([row("A", "development", .1, .1)], {"family": ["A"]})
