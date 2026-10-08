import json
from dataclasses import replace
import pandas as pd
import pytest
from quanto.backtest.engine import run_backtest
from quanto.strategies.base import Hold
from tests.fixtures.engine_cases import bundle_case, spec_case, ScriptedStrategy, target


def test_bound_features_use_only_current_bars_and_known_fact_revisions():
    from datetime import date
    from quanto.factors.features import FeatureField, FeatureSetDefinition, compute_features, make_cell
    from quanto.framework import AssetBinding, DefinitionRef
    from tests.fixtures.framework_cases import facts_bundle, view_at

    def compute(view, params):
        bars = view.bars(view.ordered_symbols, date(2024, 1, 1))
        facts = view.table("facts", view.ordered_symbols)
        result = {}
        for symbol in view.ordered_symbols:
            local = facts.loc[facts.symbol == symbol]
            if local.empty:
                continue
            bar = bars.loc[bars.symbol == symbol].iloc[-1]
            fact = local.iloc[-1]
            result[symbol] = {"synthetic_sum": make_cell(bar.close + fact.value,
                ((bar.source_available_at, bar.source_ref), (fact.source_available_at, fact.source_ref)))}
        return result, {}

    definition = FeatureSetDefinition(ref=DefinitionRef(kind="feature", id="synthetic-pit", version="1"),
        implementation_hash="f" * 64, supported_instrument_types=("etf",),
        parameter_schema={"type": "object", "properties": {}}, resolve_requirements=lambda params, basket, interval: (),
        fields=(FeatureField("synthetic_sum", "test-only combination", "test_unit", "per_symbol", "hours", "mask"),),
        compute=compute)
    original, binding, semantics = facts_bundle()
    before = compute_features(definition, view_at(original, binding, semantics), {}, None)
    original.auxiliary["facts"].loc[1, "value"] = 999999.
    original.prices.loc[original.prices.date.dt.day >= 4, ["open", "close"]] = 777.
    changed_binding = AssetBinding(**{**binding.model_dump(exclude={"binding_hash"}), "dataset_content_hash": "b" * 64})
    after = compute_features(definition, view_at(original, changed_binding, semantics), {}, None)
    assert before.per_symbol == after.per_symbol
    assert before.global_values == after.global_values
    assert before.ordered_symbols == after.ordered_symbols == ("B", "A")
    assert after.per_symbol["B"]["synthetic_sum"].value is None
    cell = after.per_symbol["A"]["synthetic_sum"]
    assert cell.value == 20.
    assert cell.source_available_at == after.signal_cutoff
    assert cell.age == 0 and len(cell.source_refs) == 2
    assert before.dataset_content_hash != after.dataset_content_hash
    assert before.frame_hash != after.frame_hash
    future = compute_features(definition, view_at(original, changed_binding, semantics, "2024-01-05"), {}, None)
    assert future.per_symbol["A"]["synthetic_sum"].value == 1000776.


def run(bundle, intents, **changes):
    return run_backtest(bundle, ScriptedStrategy(intents), spec_case(bundle, **changes), lambda event: None, lambda: False)


def test_next_open_ignores_future_close():
    original = run(bundle_case(), {1: target(A=1)})
    changed = run(bundle_case({(2, "A"): (10, 100), (3, "A"): (200, 200)}), {1: target(A=1)})
    assert original.orders.iloc[0].quantity == changed.orders.iloc[0].quantity == 100
    assert original.fills.iloc[0].price == changed.fills.iloc[0].price == 10


def test_dividend_receivable_payment_and_split():
    b = bundle_case({(3, "A"): (9, 9), (4, "A"): (9, 9), (5, "A"): (4.5, 4.5)})
    events = pd.DataFrame([
        dict(symbol="A", event_type="dividend", record_date=pd.Timestamp("2024-01-02"), ex_date=pd.Timestamp("2024-01-03"), pay_date=pd.Timestamp("2024-01-04"), cash_per_share=1, split_ratio=1),
        dict(symbol="A", event_type="split", record_date=pd.NaT, ex_date=pd.Timestamp("2024-01-05"), pay_date=pd.NaT, cash_per_share=0, split_ratio=2)])
    result = run(replace(b, events=events), {1: target(A=1)})
    ex_day, pay_day, split_day = result.equity.iloc[2], result.equity.iloc[3], result.equity.iloc[4]
    assert ex_day.receivables == 100 and ex_day.cash == 0 and ex_day.equity == 1000
    assert pay_day.receivables == 0 and pay_day.cash == 100
    assert result.positions.iloc[-1].shares == 200 and split_day.equity == 1000


def test_empty_target_liquidates_hold_retains_and_last_order_is_unexecuted():
    held = run(bundle_case(), {1: target(A=1), 3: Hold("保持")})
    exited = run(bundle_case(), {1: target(A=1), 3: target()})
    last = run(bundle_case(), {5: target(A=1)})
    assert held.positions.iloc[-1].shares == 100 and exited.positions.iloc[-1].shares == 0
    assert last.orders.iloc[0].status == "unexecuted"
    assert last.fills.empty and "price" in last.fills


def test_risk_close_only_sells_next_open_and_retries_actual_remaining():
    b = bundle_case({(3, "A"): (10, 8), (4, "A"): (8, 8), (5, "A"): (8, 8)})
    b.status.loc[b.status.date.dt.day == 4, ["status", "can_buy", "can_sell"]] = ["suspended", False, False]
    result = run(b, {1: target(A=1), 4: target(A=1)}, risk={"drawdown_pause": .15, "pause_action": "liquidate"})
    sells = result.orders[result.orders.side == "sell"]
    assert sells.quantity.tolist() == [100, 100]
    assert sells.status.tolist() == ["expired", "filled"]
    assert result.fills.trade_date.dt.day.tolist() == [2, 5]


def test_warmup_uses_observed_prices_and_runtime_schedule():
    b = bundle_case()
    b = replace(b, calendar=pd.DataFrame({"date": pd.bdate_range("2023-12-01", "2024-01-15")}))
    strategy = ScriptedStrategy({})
    strategy.warmup_bars = 5
    with pytest.raises(ValueError, match="warmup"):
        run_backtest(b, strategy, spec_case(b), lambda e: None, lambda: False)
    strategy.warmup_bars = 0
    strategy.evaluate = False
    with pytest.raises(ValueError, match="NO_EVALUATION"):
        run_backtest(b, strategy, spec_case(b), lambda e: None, lambda: False)


def test_completed_day_payload_survives_strategy_exception():
    events = []
    class Broken(ScriptedStrategy):
        def on_close(self, ctx):
            if ctx.asof.day == 3:
                raise RuntimeError("failure")
            return super().on_close(ctx)
    b = bundle_case()
    with pytest.raises(RuntimeError):
        run_backtest(b, Broken({1: target(A=1)}), spec_case(b), events.append, lambda: False)
    assert events[-1].completed_days == 2
    payload = events[-1].payload
    json.dumps(payload, allow_nan=False)
    assert payload["completed_through"] == "2024-01-02"
    assert payload["tables"]["orders"][0]["status"] == "filled"
    assert len(payload["tables"]["equity"]) == 1


def test_pending_buy_split_preserves_signal_notional():
    b = bundle_case({(2, "A"): (5, 5), (3, "A"): (5, 5), (4, "A"): (5, 5), (5, "A"): (5, 5)})
    events = pd.DataFrame([dict(symbol="A", event_type="split", record_date=pd.NaT, ex_date=pd.Timestamp("2024-01-02"), pay_date=pd.NaT, cash_per_share=0, split_ratio=2)])
    result = run(replace(b, events=events), {1: target(A=1)})
    assert result.orders.iloc[0].quantity == 200 and result.fills.iloc[0].quantity == 200
    assert result.equity.equity.tolist() == [1000] * 5


def test_dividend_entitlement_survives_ex_day_sale_and_adjusted_mode_skips_income():
    b = bundle_case({(3, "A"): (9, 9), (4, "A"): (9, 9)})
    events = pd.DataFrame([dict(symbol="A", event_type="dividend", record_date=pd.Timestamp("2024-01-02"), ex_date=pd.Timestamp("2024-01-03"), pay_date=pd.Timestamp("2024-01-04"), cash_per_share=1, split_ratio=1)])
    b = replace(b, events=events)
    result = run(b, {1: target(A=1), 2: target(A=.6)})
    assert result.fills.iloc[1].quantity == 40
    assert result.equity.iloc[2].receivables == 100 and result.equity.iloc[2].cash == 360
    assert result.equity.iloc[3].cash == 460
    adjusted = replace(b, manifest=b.manifest.model_copy(update={"price_mode": "adjusted_research", "limitations": ["研究价格"]}))
    assert run(adjusted, {1: target(A=1)}).equity.receivables.sum() == 0


def test_cash_interest_uses_start_of_day_cash_and_next_day_sale_proceeds():
    b = bundle_case()
    result = run(b, {1: target(A=.9), 2: target()}, metrics={"cash_rate": 2.52})
    interest = result.account_events[result.account_events.event_type == "interest"]
    assert interest.amount.tolist() == pytest.approx([10, 10.1, 1.201, 10.21301, 10.3151401], abs=1e-8)
    assert result.equity.cash.iloc[-1] == pytest.approx(1041.8291501, abs=1e-8)


def test_midweek_truncation_and_cash_reports_are_valid():
    b = bundle_case(days=3)
    strategy = ScriptedStrategy({})
    result = run_backtest(b, strategy, spec_case(b), lambda e: None, lambda: False)
    assert not any(ctx.is_week_end for ctx in strategy.contexts)
    assert result.fills.empty and len(result.equity) == 3
    terminal = replace(b, calendar=b.calendar.iloc[:3])
    strategy = ScriptedStrategy({})
    run_backtest(terminal, strategy, spec_case(b), lambda e: None, lambda: False)
    assert not strategy.contexts[-1].is_week_end


def test_future_approvals_are_not_visible_to_strategy():
    b = bundle_case()
    approvals = pd.DataFrame([dict(approval_id="future", pause_asof="2024-01-02", available_at="2024-01-03T08:00:00Z")])
    strategy = ScriptedStrategy({})
    run_backtest(replace(b, auxiliary={"risk_approvals": approvals}), strategy, spec_case(b), lambda e: None, lambda: False)
    assert strategy.contexts[2].risk_state["risk_approvals"] == ()
    assert len(strategy.contexts[3].risk_state["risk_approvals"]) == 1


def test_cancel_preserves_only_completed_days_and_tables_conserve_equity():
    from quanto.backtest.models import BacktestCancelled
    events = []
    b = bundle_case()
    with pytest.raises(BacktestCancelled):
        run_backtest(b, ScriptedStrategy({1: target(A=1)}), spec_case(b), events.append, lambda: len(events) == 2)
    assert [event.completed_days for event in events] == [1, 2]
    for event in events:
        row = event.payload["tables"]["equity"][0]
        assert row["cash"] + row["receivables"] + row["market_value"] == pytest.approx(row["equity"], abs=1e-8)


def test_ineligible_holding_is_valued_but_new_buy_is_cancelled():
    b = bundle_case()
    b.eligibility.loc[b.eligibility.date.dt.day >= 3, "eligible"] = False
    retained = run(b, {1: target(A=1)})
    assert retained.equity.equity.tolist() == [1000] * 5
    blocked = run(b, {3: target(A=1)})
    assert blocked.fills.empty and blocked.orders.iloc[0].reason == "ineligible"


def test_daily_risk_runs_when_strategy_does_not_evaluate():
    class Once(ScriptedStrategy):
        def should_evaluate(self, ctx):
            return ctx.asof.day == 1
    b = bundle_case({(3, "A"): (10, 8), (4, "A"): (8, 8)})
    result = run_backtest(b, Once({1: target(A=1)}), spec_case(b, risk={"drawdown_pause": .15, "pause_action": "liquidate"}), lambda e: None, lambda: False)
    assert result.fills.trade_date.dt.day.tolist() == [2, 4]
    assert result.fills.side.tolist() == ["buy", "sell"]


def test_start_after_record_date_has_no_dividend_entitlement():
    b = bundle_case({(3, "A"): (9, 9), (4, "A"): (9, 9)})
    events = pd.DataFrame([dict(symbol="A", event_type="dividend", record_date=pd.Timestamp("2024-01-02"), ex_date=pd.Timestamp("2024-01-03"), pay_date=pd.Timestamp("2024-01-04"), cash_per_share=1, split_ratio=1)])
    result = run(replace(b, events=events), {3: target(A=1)}, start="2024-01-03")
    assert result.account_events.loc[result.account_events.event_type == "dividend_payment", "amount"].sum() == 0
    assert result.equity.receivables.sum() == 0


def test_signal_history_snapshots_reused_nested_strategy_dicts():
    from quanto.strategies.base import TargetPortfolio

    class Reused(ScriptedStrategy):
        def __init__(self):
            self.weights, self.reasons, self.diagnostics = {"A": .5}, {"A": "first"}, {"trace": {"days": []}}

        def should_evaluate(self, ctx):
            return True

        def on_close(self, ctx):
            self.diagnostics["trace"]["days"].append(ctx.asof.day)
            if ctx.asof.day > 1:
                self.weights["A"], self.reasons["A"] = .2, "later"
            return TargetPortfolio(self.weights, self.reasons, self.diagnostics)

    b, events = bundle_case(), []
    result = run_backtest(b, Reused(), spec_case(b), events.append, lambda: False)
    first = result.signals.iloc[0]
    assert first.weights == {"A": .5} and first.reasons == {"A": "first"}
    assert first.diagnostics == {"trace": {"days": [1]}}
    assert events[0].payload["tables"]["signals"][0]["diagnostics"] == first.diagnostics
    assert events[0].payload["tables"]["signals"][0]["weights"] == first.weights


def test_first_day_negative_interest_counts_toward_drawdown():
    result = run(bundle_case(), {1: target(A=1)}, metrics={"cash_rate": -.0252}, risk={"drawdown_pause": .00005})
    assert result.equity.iloc[0].equity == pytest.approx(999.9, abs=1e-8)
    pauses = result.risk_events[result.risk_events.code == "DRAWDOWN_PAUSE"]
    assert pauses.iloc[0].date == pd.Timestamp("2024-01-01")
    assert result.fills.empty


def test_negative_interest_reserves_cash_before_buys_and_posts_at_close():
    result = run(bundle_case(), {1: target(A=1)}, metrics={"cash_rate": -2.52})
    assert (result.equity.cash >= 0).all()
    assert result.fills.iloc[0].quantity == 98
    assert result.equity.iloc[1].cash == pytest.approx(.1, abs=1e-8)
    interest = result.account_events[result.account_events.event_type == "interest"]
    assert interest.amount.iloc[:2].tolist() == pytest.approx([-10, -9.9], abs=1e-8)
    assert interest.details.iloc[1]["opening_cash"] == 990


def test_unaffordable_interest_charge_fails_without_completed_day():
    b, events = bundle_case(), []
    with pytest.raises(ValueError, match="UNFUNDED_INTEREST"):
        run_backtest(b, ScriptedStrategy({1: target(A=1)}), spec_case(b, metrics={"cash_rate": -504}), events.append, lambda: False)
    assert events == []


def test_strategy_cannot_mutate_group_or_approval_facts_before_policy():
    class MutationAttempt(ScriptedStrategy):
        def on_close(self, ctx):
            with pytest.raises(TypeError):
                ctx.risk_state["symbol_groups"]["A"] = "bypass"
            with pytest.raises(TypeError):
                ctx.risk_state["risk_approvals"][0]["pause_asof"] = ctx.risk_state["pause_asof"]
            with pytest.raises(AttributeError):
                ctx.risk_state["risk_approvals"].append(dict(approval_id="invented", pause_asof=ctx.risk_state["pause_asof"], available_at="2024-01-01T06:00:00Z"))
            return target(A=1)

    b = bundle_case({(3, "A"): (10, 5)})
    approvals = pd.DataFrame([dict(approval_id="prior", pause_asof="2023-12-29", available_at="2024-01-01T06:00:00Z")])
    result = run_backtest(replace(b, auxiliary={"risk_approvals": approvals}), MutationAttempt({}),
        spec_case(b, risk={"group_caps": {"G": .25}, "drawdown_pause": .1, "pause_action": "liquidate", "recovery_mode": "recorded_approval"}), lambda e: None, lambda: False)
    assert result.signals.iloc[0].weights == {"A": 1}
    assert result.signals.iloc[0].approved_weights == {"A": .25}
    assert result.fills.iloc[0].quantity == 25
    assert "RISK_RESUMED" not in result.risk_events.code.tolist()
    assert result.positions.iloc[-1].shares == 0
