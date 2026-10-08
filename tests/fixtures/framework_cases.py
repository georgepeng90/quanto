"""Explicit identities for framework tests; never registered as production definitions."""
from datetime import datetime, timezone
from dataclasses import replace
from datetime import date

import pandas as pd

from quanto.data.contracts import InstrumentId
from quanto.backtest.dataset_identity import standard_table_semantics
from quanto.data.point_in_time import TableSemantics, _bind_point_in_time, session_cutoff
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding, TableSemanticBinding
from tests.fixtures.engine_cases import bundle_case



def instrument_values(code="000001", instrument_type="etf", exchange="SZSE"):
    return dict(exchange=exchange, instrument_type=instrument_type, code=code)


def basket_values(members=None):
    return dict(basket_id="basket-1", revision=1,
                members=members or [instrument_values(), instrument_values("000002")],
                created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), selection_note="Explicit synthetic selection")


def binding_values():
    return dict(schema_version=1, dataset_version_id="dataset-1", dataset_content_hash="a" * 64,
                assets=[dict(instrument_id=instrument_values(code), local_symbol=code,
                             evidence_ref=f"source:{code}") for code in ("000001", "000002")],
                benchmark=dict(instrument_id=instrument_values("000300", "index", "SSE"),
                               return_basis="price_index", evidence_ref="source:benchmark"),
                table_semantics=[dict(table="prices", semantic_id="daily-bars", version="1",
                                      schema_definition={"close": {"dtype": "float", "unit": "CNY"}},
                                      temporal_rules={"visibility": "session_close", "keys": ["symbol", "date"]},
                                      evidence_ref="research:session-close")])


def backtest_input_values():
    return dict(start="2024-01-01", end="2024-06-30", initial_cash=10000., seed=42,
                execution=dict(commission_rate=.0003, minimum_commission=0., slippage_rate=.0005,
                               max_stale_sessions=2, model="next_open"),
                risk=dict(max_total_weight=1., max_symbol_weight=1., group_caps={}, stress_scenarios=[],
                          pause_action="block_new", recovery_mode="never"),
                metrics=dict(annual_sessions=252, risk_free_rate=0., cash_rate=0., definition_version="1"))


def fixed_request_values():
    return dict(framework_contract_version="multi-asset-v1", basket_revision_ref=dict(basket_id="basket-1", revision=1),
                dataset_version_id="dataset-1", strategy_revision_id="strategy-1",
                trading_definition_ref=dict(kind="trading", id="etf", version="1"),
                strategy_params={}, backtest_spec=backtest_input_values(), sample_purpose="research")


def model_request_values():
    return dict(framework_contract_version="multi-asset-v1", basket_revision_ref=dict(basket_id="basket-1", revision=1),
                dataset_id="dataset-1", learning_definition_ref=dict(kind="learning", id="etf-ppo", version="1"),
                trading_definition_ref=dict(kind="trading", id="etf", version="1"),
                spec=dict(name="ETF fixture", backtest=backtest_input_values(), lookback=5,
                          train_start="2024-01-01", train_end="2024-03-29",
                          validation_start="2024-04-01", validation_end="2024-06-28"))


# Explicit, synthetic temporal facts shared by PIT and feature integration tests.
def facts_bundle(rows=None, rule_changes=None):
    bundle = bundle_case(symbols=("A", "B"))
    rows = rows if rows is not None else [
        dict(symbol="A", period_end="2023-12-31", revision_id="r1", revision_sequence=1, available_at="2024-01-02T06:59:00Z", value=10.),
        dict(symbol="A", period_end="2023-12-31", revision_id="r2", revision_sequence=2, available_at="2024-01-04T07:01:00Z", value=20.),
    ]
    rules = dict(availability_rule="timestamp", available_column="available_at", symbol_column="symbol", business_keys=["symbol", "period_end"],
                 revision_policy="sequence", revision_id_column="revision_id", revision_order_column="revision_sequence")
    rules.update(rule_changes or {})
    declaration = TableSemanticBinding(table="facts", semantic_id="fixture-facts", version="1",
        schema_definition={"value": {"dtype": "float", "unit": "score"}}, temporal_rules=rules, evidence_ref="fixture:pit")
    bundle = replace(bundle, auxiliary={"facts": pd.DataFrame(rows)})
    binding = AssetBinding(dataset_version_id="fixture", dataset_content_hash="a" * 64,
        assets=tuple(AssetMapping(instrument_id=InstrumentId(exchange="SSE", instrument_type="etf", code=symbol), local_symbol=symbol, evidence_ref="fixture:pit") for symbol in ("A", "B")),
        benchmark=BenchmarkBinding(custom_series_id="fixture-benchmark", return_basis="price_index", evidence_ref="fixture:pit"),
        table_semantics=(*standard_table_semantics(bundle, "fixture:pit"), declaration))
    semantics = tuple(TableSemantics.from_binding(item, supported_versions={("fixture-facts", "1")}) for item in binding.table_semantics)
    return bundle, binding, semantics


def tables(bundle):
    return {**{name: getattr(bundle, name) for name in ("prices", "benchmark", "calendar", "instruments", "status", "events")}, **bundle.auxiliary}


def view_at(bundle, binding, semantics, day="2024-01-03", cutoff=None):
    return _bind_point_in_time(tables(bundle), binding, ("B", "A"), cutoff or session_cutoff(date.fromisoformat(day)), semantics=semantics)
