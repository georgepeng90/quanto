from dataclasses import replace

import pandas as pd
import pytest

from quanto.backtest.dataset_identity import standard_table_semantics
from quanto.backtest.engine import SessionRunner, run_backtest
from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.data.contracts import InstrumentId
from quanto.factors.features import FeatureField, FeatureSetDefinition
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding, DefinitionRef
from quanto.strategies.base import Hold
from tests.fixtures.engine_cases import ScriptedStrategy, bundle_case, spec_case, target


def binding_for(bundle, instrument_type="etf"):
    return AssetBinding(dataset_version_id="runtime-fixture", dataset_content_hash="a" * 64,
        assets=tuple(AssetMapping(instrument_id=InstrumentId(exchange="SSE", instrument_type=instrument_type, code=symbol),
            local_symbol=symbol, evidence_ref="fixture:runtime") for symbol in bundle.instruments.symbol),
        benchmark=BenchmarkBinding(custom_series_id="fixture", return_basis="price_index", evidence_ref="fixture:runtime"),
        table_semantics=standard_table_semantics(bundle, "fixture:runtime"))


def test_runner_exposes_causal_features_and_frozen_instruments_without_changing_eligibility():
    bundle = bundle_case(symbols=("A", "B"))
    feature = FeatureSetDefinition(ref=DefinitionRef(kind="feature", id="masked", version="1"),
        implementation_hash="b" * 64, supported_instrument_types=("etf",),
        parameter_schema={"type": "object", "properties": {}}, resolve_requirements=lambda params, basket, interval: (),
        fields=(FeatureField("missing", "fixture", "ratio", "per_symbol", "days", "mask"),),
        compute=lambda view, params: ({}, {}))
    spec = spec_case(bundle, symbols=["B", "A"])
    runner = SessionRunner(bundle, spec, lambda: False, asset_binding=binding_for(bundle), feature_definition=feature)
    ctx = runner.advance_to_close()
    assert ctx.data_view.ordered_symbols == ctx.features.ordered_symbols == ("B", "A")
    assert ctx.data_view.bars(("A",), spec.start).date.max() == ctx.asof
    assert ctx.features.per_symbol["A"]["missing"].value is None
    assert set(ctx.eligible_symbols) == {"A", "B"}
    assert ctx.instruments["A"]["instrument_id"]["instrument_type"] == "etf"
    with pytest.raises(TypeError):
        ctx.instruments["A"]["lot_size"] = 999
    runner.submit_close_intent(target(A=1))
    next_ctx = runner.advance_to_close()
    assert next_ctx.portfolio.shares["A"] == 100


@pytest.mark.parametrize("state", ["unknown", "delisted", None])
def test_unknown_or_missing_daily_state_blocks_even_without_a_strategy_order(state):
    bundle = bundle_case()
    if state is None:
        bundle = replace(bundle, status=bundle.status[bundle.status.date.dt.day != 2])
    else:
        bundle.status.loc[bundle.status.date.dt.day == 2, "status"] = state
    runner = SessionRunner(bundle, spec_case(bundle), lambda: False)
    runner.advance_to_close()
    runner.submit_close_intent(Hold("idle"))
    with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_MISSING"):
        runner.advance_to_close()
    assert runner.completed_through == pd.Timestamp("2024-01-01")


@pytest.mark.parametrize("mode", ["raw_cash", "adjusted_research"])
def test_unknown_exit_event_cannot_produce_a_complete_return_series(mode):
    bundle = bundle_case()
    event = dict(symbol="A", event_type="liquidation", record_date=pd.NaT, ex_date=pd.Timestamp("2024-01-03"),
                 pay_date=pd.NaT, cash_per_share=0, split_ratio=1)
    bundle = replace(bundle, manifest=bundle.manifest.model_copy(update={"price_mode": mode}), events=pd.DataFrame([event]))
    with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_MISSING"):
        run_backtest(bundle, ScriptedStrategy({}), spec_case(bundle), lambda event: None, lambda: False)


def test_etf_definition_does_not_silently_operate_stock_bindings():
    bundle = bundle_case()
    with pytest.raises(ValueError, match="EXECUTION_CAPABILITY_MISSING"):
        SessionRunner(bundle, spec_case(bundle), lambda: False, asset_binding=binding_for(bundle, "stock"))


def test_status_is_required_only_after_evidenced_listing_date():
    bundle = bundle_case()
    bundle.instruments.loc[:, "listing_date"] = pd.Timestamp("2024-01-03")
    bundle = replace(bundle, prices=bundle.prices[bundle.prices.date.dt.day >= 3],
        status=bundle.status[bundle.status.date.dt.day >= 3],
        eligibility=bundle.eligibility[bundle.eligibility.date.dt.day >= 3])
    runner = SessionRunner(bundle, spec_case(bundle), lambda: False)
    for _ in runner.dates:
        ctx = runner.advance_to_close()
        runner.submit_close_intent(Hold("idle"))
    assert ctx.asof == pd.Timestamp("2024-01-05")


@pytest.mark.parametrize("mode, expected_cash, dividends", [("raw_cash", 102., 1), ("adjusted_research", 3., 0)])
def test_injected_synthetic_stock_uses_one_fee_and_event_ledger(mode, expected_cash, dividends):
    # An internal interface fixture, not a production stock execution definition.
    definition = replace(ETF_TRADING_DEFINITION, ref=DefinitionRef(kind="trading", id="synthetic-stock", version="1"),
        implementation_hash="c" * 64, supported_instrument_types=("stock",),
        fee_hook=lambda fill: {"commission": 7. if fill["quantity"] else 0.})
    bundle = bundle_case()
    events = pd.DataFrame([dict(symbol="A", event_type="dividend", record_date=pd.Timestamp("2024-01-02"),
        ex_date=pd.Timestamp("2024-01-03"), pay_date=pd.Timestamp("2024-01-04"), cash_per_share=1., split_ratio=1.)])
    bundle = replace(bundle, events=events, manifest=bundle.manifest.model_copy(update={"price_mode": mode}))
    result = run_backtest(bundle, ScriptedStrategy({1: target(A=1)}), spec_case(bundle), lambda event: None, lambda: False,
        trading_definition=definition, asset_binding=binding_for(bundle, "stock"))
    assert result.fills.quantity.tolist() == [99]
    assert result.fills.commission.tolist() == [7.]
    assert result.equity.iloc[-1].cash == expected_cash
    assert result.positions.iloc[-1].shares == 99
    assert (result.account_events.event_type == "dividend_payment").sum() == dividends
    assert (result.account_events.event_type == "dividend_receivable").sum() == dividends


def test_injected_eligibility_can_tighten_observed_buy_permission():
    definition = replace(ETF_TRADING_DEFINITION, implementation_hash="d" * 64,
        eligibility_hook=lambda state: {"can_buy": False, "can_sell": state["can_sell"], "reason": "fixture restriction"})
    bundle = bundle_case()
    result = run_backtest(bundle, ScriptedStrategy({1: target(A=1)}), spec_case(bundle), lambda event: None, lambda: False,
        trading_definition=definition, asset_binding=binding_for(bundle))
    assert result.fills.empty
    assert result.orders.iloc[0].reason == "fixture restriction"
    assert result.equity.equity.tolist() == [1000.] * 5


def test_future_exit_event_does_not_change_a_completed_earlier_interval():
    bundle = bundle_case()
    event = dict(symbol="A", event_type="liquidation", record_date=pd.NaT, ex_date=pd.Timestamp("2024-01-12"),
                 pay_date=pd.NaT, cash_per_share=0, split_ratio=1)
    bundle = replace(bundle, events=pd.DataFrame([event]))
    result = run_backtest(bundle, ScriptedStrategy({}), spec_case(bundle), lambda event: None, lambda: False)
    assert result.completed_through == pd.Timestamp("2024-01-05")
