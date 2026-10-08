"""Explicit identities for framework tests; never registered as production definitions."""
from datetime import datetime, timezone


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
