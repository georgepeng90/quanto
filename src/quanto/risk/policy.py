from dataclasses import dataclass
import copy

import pandas as pd

from quanto.config import RiskSpec
from quanto.strategies.base import Hold, StrategyContext, TargetPortfolio


@dataclass(frozen=True)
class RiskDecision:
    intent: Hold | TargetPortfolio
    state: dict
    events: list[dict]


class RiskPolicy:
    def __init__(self, spec: RiskSpec, initial_equity: float = 0.0):
        self.spec = spec
        self.state = dict(paused=False, pause_asof=None, risk_reference=initial_equity)
        self.consumed_approvals: set[str] = set()

    def _limited(self, weights, groups):
        weights = {s: min(w, self.spec.max_symbol_weight) for s, w in weights.items() if w > 0}
        total = sum(weights.values())
        if total > self.spec.max_total_weight:
            weights = {s: w * self.spec.max_total_weight / total for s, w in weights.items()}
        for group, cap in sorted(self.spec.group_caps.items()):
            exposure = sum(w for s, w in weights.items() if groups.get(s) == group)
            if exposure > cap:
                weights = {s: w * cap / exposure if groups.get(s) == group else w for s, w in weights.items()}
        if self.spec.max_stress_loss is not None:
            for scenario in self.spec.stress_scenarios:
                loss = max(0, sum(w * (-scenario.symbol_shocks.get(s, 0) + scenario.execution_cost_rate) for s, w in weights.items()))
                if loss > self.spec.max_stress_loss:
                    weights = {s: w * self.spec.max_stress_loss / loss for s, w in weights.items()}
        return {s: w for s, w in weights.items() if w > 0}

    @staticmethod
    def _same(left, right):
        return all(abs(left.get(s, 0) - right.get(s, 0)) <= 1e-12 for s in left.keys() | right.keys())

    def evaluate(self, ctx: StrategyContext, proposed: Hold | TargetPortfolio) -> RiskDecision:
        events = []
        asof = ctx.asof.date().isoformat()
        groups = ctx.risk_state.get("symbol_groups", {})
        actual = {s: v / ctx.portfolio.equity for s, v in ctx.portfolio.market_values.items() if v > 0} if ctx.portfolio.equity > 0 else {}
        requested = actual if isinstance(proposed, Hold) else proposed.weights
        limited_actual = self._limited(actual, groups)
        actual_breach = not self._same(actual, limited_actual)
        limited_requested = self._limited(requested, groups)

        def event(code, message, **details):
            events.append(dict(date=ctx.asof, code=code, message=message, details=copy.deepcopy(details)))

        reference = max(self.state["risk_reference"], ctx.portfolio.equity)
        self.state["risk_reference"] = reference
        drawdown = 1 - ctx.portfolio.equity / reference if reference else 0
        if not self.state["paused"] and self.spec.drawdown_pause is not None and drawdown + 1e-12 >= self.spec.drawdown_pause:
            self.state.update(paused=True, pause_asof=asof)
            event("DRAWDOWN_PAUSE", "回撤触发风险暂停", drawdown=drawdown)

        if self.state["paused"] and self.spec.recovery_mode == "recorded_approval":
            cutoff = (ctx.asof.normalize() + pd.Timedelta(hours=15)).tz_localize("Asia/Shanghai").tz_convert("UTC")
            for approval in ctx.risk_state.get("risk_approvals", ()):
                identifier = str(approval["approval_id"])
                available = pd.Timestamp(approval["available_at"])
                if available.tzinfo is None:
                    raise ValueError("INVALID_APPROVAL_TIME: 批准时间必须包含时区")
                if (identifier not in self.consumed_approvals and str(approval["pause_asof"])[:10] == self.state["pause_asof"]
                    and available <= cutoff and asof > self.state["pause_asof"]
                    and not actual_breach and self._same(requested, limited_requested)):
                    self.consumed_approvals.add(identifier)
                    self.state.update(paused=False, pause_asof=None, risk_reference=ctx.portfolio.equity)
                    event("RISK_RESUMED", "已接受本次暂停的恢复批准", approval_id=identifier)
                    break

        if self.state["paused"]:
            if self.spec.pause_action == "liquidate":
                allowed = {}
            else:
                allowed = {s: min(w, requested.get(s, 0)) for s, w in limited_actual.items()}
                if self.spec.pause_action == "reduce":
                    total = sum(allowed.values())
                    if total > self.spec.pause_target_weight:
                        allowed = {s: w * self.spec.pause_target_weight / total for s, w in allowed.items()}
            intent = TargetPortfolio(allowed, {s: "risk_pause" for s in actual}, {})
        elif actual_breach:
            # First repair actual exposure; a fresh target cannot refill the breach.
            intent = TargetPortfolio(limited_actual, {s: "risk_limit" for s in actual}, {})
            event("EXPOSURE_REDUCTION", "实际持仓超出风险限额", actual=actual, allowed=limited_actual)
        elif not self._same(requested, limited_requested):
            intent = TargetPortfolio(limited_requested, {s: "risk_limit" for s in requested}, {})
            event("TARGET_LIMITED", "目标持仓已按风险限额缩减", requested=requested, allowed=limited_requested)
        else:
            intent = proposed
        return RiskDecision(intent, dict(self.state), events)
