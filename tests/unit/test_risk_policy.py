import pandas as pd
import pytest
from quanto.risk.policy import RiskPolicy
from quanto.strategies.base import Hold, PortfolioView, StrategyContext
from tests.fixtures.engine_cases import bundle_case, spec_case, target


def context(day, equity=1000, weights=None, approvals=()):
    weights = weights or {}
    values = {s: equity * w for s, w in weights.items()}
    return StrategyContext(pd.Timestamp(f"2024-01-{day:02}"), pd.DataFrame(columns=["date"]), ("A", "B"),
        PortfolioView(equity - sum(values.values()), 0, {s: 100 for s in weights}, {s: 100 for s in weights}, values, equity, 0),
        (), {"symbol_groups": {"A": "G", "B": "G"}, "risk_approvals": list(approvals)}, False)


def test_caps_group_and_stress_apply_to_actual_holdings_daily():
    risk = spec_case(bundle_case(), risk={"max_symbol_weight": .6, "group_caps": {"G": .5},
        "stress_scenarios": [{"name": "shock", "symbol_shocks": {"A": -.4, "B": -.4}, "execution_cost_rate": 0}], "max_stress_loss": .1}).risk
    result = RiskPolicy(risk).evaluate(context(1, weights={"A": .5, "B": .5}), Hold("保持"))
    assert result.intent.weights == pytest.approx({"A": .125, "B": .125}, abs=1e-8)
    assert result.events


def test_pause_approval_episode_timing_and_fresh_reference():
    policy = RiskPolicy(spec_case(bundle_case(), risk={"drawdown_pause": .2, "pause_action": "liquidate", "recovery_mode": "recorded_approval"}).risk)
    policy.evaluate(context(1), Hold("保持"))
    paused = policy.evaluate(context(2, 800, {"A": 1}), target(A=1))
    assert paused.state["paused"] and paused.intent.weights == {}
    approvals = [dict(approval_id="old", pause_asof="2024-01-01", available_at="2024-01-03T01:00:00Z"),
                 dict(approval_id="fresh", pause_asof="2024-01-02", available_at="2024-01-03T08:00:00Z")]
    assert policy.evaluate(context(3, 800, approvals=approvals), target(A=1)).state["paused"]
    resumed = policy.evaluate(context(4, 800, approvals=approvals), target(A=1))
    assert not resumed.state["paused"] and resumed.state["risk_reference"] == 800
    assert resumed.intent.weights == {"A": 1}
    again = policy.evaluate(context(5, 640, {"A": 1}, approvals), target(A=1))
    assert again.state["paused"] and again.state["pause_asof"] == "2024-01-05"


def test_approval_cannot_resume_target_which_breaches_limits():
    policy = RiskPolicy(spec_case(bundle_case(), risk={"max_symbol_weight": .5, "drawdown_pause": .2,
        "pause_action": "liquidate", "recovery_mode": "recorded_approval"}).risk)
    policy.evaluate(context(1), Hold("保持"))
    policy.evaluate(context(2, 800), Hold("保持"))
    approval = [dict(approval_id="ok", pause_asof="2024-01-02", available_at="2024-01-03T07:00:00Z")]
    assert policy.evaluate(context(3, 800, approvals=approval), target(A=1)).state["paused"]
    assert not policy.evaluate(context(4, 800, approvals=approval), target(A=.5)).state["paused"]


@pytest.mark.parametrize("action,expected", [("block_new", .8), ("reduce", .3), ("liquidate", 0)])
def test_pause_actions_effective_and_never_recovery_persists(action, expected):
    settings = dict(drawdown_pause=.2, pause_action=action)
    if action == "reduce":
        settings["pause_target_weight"] = .3
    policy = RiskPolicy(spec_case(bundle_case(), risk=settings).risk)
    policy.evaluate(context(1), Hold("保持"))
    decision = policy.evaluate(context(2, 800, {"A": .8}), target(A=1))
    assert sum(decision.intent.weights.values()) == pytest.approx(expected, abs=1e-8)
    assert policy.evaluate(context(5, 1200), target(A=1)).state["paused"]


def test_initial_equity_reference_counts_the_first_close_loss():
    policy = RiskPolicy(spec_case(bundle_case(), risk={"drawdown_pause": .00005}).risk, initial_equity=1000)
    decision = policy.evaluate(context(1, 999.9), Hold("保持"))
    assert decision.state["paused"]
    assert decision.state["risk_reference"] == 1000
    assert decision.events[0]["details"]["drawdown"] == pytest.approx(.0001, abs=1e-8)


def test_risk_event_snapshots_requested_weights():
    weights = {"A": 1.0}
    from quanto.strategies.base import TargetPortfolio

    decision = RiskPolicy(spec_case(bundle_case(), risk={"max_symbol_weight": .25}).risk).evaluate(
        context(1), TargetPortfolio(weights, {}, {}))
    weights["A"] = .1
    assert decision.events[0]["details"]["requested"] == {"A": 1.0}
