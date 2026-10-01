from __future__ import annotations

import json

import pandas as pd
import pytest

from tests.fixtures.backtest_cases import dataset_case
from quanto.config import BacktestSpec, validate_parameters
from quanto.backtest.dataset_loaders import load_dataset, validate_dataset
from quanto.sample_data import generate_sample_data


@pytest.mark.parametrize("price_mode", ["raw_cash", "adjusted_research"])
def test_dataset_keeps_leading_zero_symbols(tmp_path, price_mode):
    path = dataset_case(tmp_path, price_mode)
    loaded = load_dataset(path)

    assert loaded.prices.iloc[0]["symbol"] == "000001"
    assert loaded.prices.shape[0] == 3
    assert loaded.price_mode == price_mode
    assert loaded.calendar.shape[0] == 5
    assert loaded.eligibility["eligible"].tolist() == [False, False, True]


@pytest.mark.parametrize("mutation", ["duplicate", "zero_price", "crossed_range", "missing_column", "unknown_gap"])
def test_invalid_dataset_is_rejected(tmp_path, mutation):
    path = dataset_case(tmp_path)
    prices_path = path / "prices.csv"
    prices = pd.read_csv(prices_path, dtype={"symbol": str})
    if mutation == "duplicate":
        prices = pd.concat([prices, prices.iloc[[0]]], ignore_index=True)
    elif mutation == "zero_price":
        prices.loc[0, "open"] = 0
    elif mutation == "crossed_range":
        prices.loc[0, "high"] = 0.5
    elif mutation == "missing_column":
        prices = prices.drop(columns="amount")
    else:
        prices = prices.iloc[:2]
    prices.to_csv(prices_path, index=False)

    with pytest.raises(ValueError):
        load_dataset(path)


def test_dataset_validation_returns_independent_tables(tmp_path):
    bundle = load_dataset(dataset_case(tmp_path))
    checked = validate_dataset(bundle)
    checked.prices.loc[0, "close"] = 999
    assert bundle.prices.loc[0, "close"] == 1.0


def test_confirmed_suspension_can_omit_quote(tmp_path):
    path = dataset_case(tmp_path)
    prices = pd.read_csv(path / "prices.csv", dtype={"symbol": str})
    prices.iloc[:2].to_csv(path / "prices.csv", index=False)
    status = pd.read_csv(path / "status.csv", dtype={"symbol": str})
    status.loc[2, ["status", "can_buy", "can_sell"]] = ["suspended", False, False]
    status.to_csv(path / "status.csv", index=False)
    bundle = load_dataset(path)
    assert bundle.prices.shape[0] == 2
    assert bundle.calendar.shape[0] == 5


def test_missing_benchmark_session_is_rejected(tmp_path):
    path = dataset_case(tmp_path)
    benchmark = pd.read_csv(path / "benchmark.csv")
    benchmark.iloc[[0, 2]].to_csv(path / "benchmark.csv", index=False)
    with pytest.raises(ValueError):
        load_dataset(path)


def test_fractional_lot_size_is_rejected(tmp_path):
    path = dataset_case(tmp_path)
    instruments = pd.read_csv(path / "instruments.csv", dtype={"symbol": str})
    instruments["lot_size"] = instruments["lot_size"].astype(float)
    instruments.loc[0, "lot_size"] = 100.5
    instruments.to_csv(path / "instruments.csv", index=False)
    with pytest.raises(ValueError):
        load_dataset(path)


def test_unknown_configuration_is_rejected():
    valid = {
        "start": "2024-01-01", "end": "2024-01-05", "symbols": ["000001"],
        "initial_cash": 100_000, "seed": 1,
        "execution": {"commission_rate": 0.0005, "minimum_commission": 5, "slippage_rate": 0.0005, "max_stale_sessions": 2, "model": "next_open"},
        "risk": {"max_total_weight": 1, "max_symbol_weight": 0.2, "group_caps": {}, "stress_scenarios": [], "max_stress_loss": None, "drawdown_pause": None, "pause_action": "block_new", "pause_target_weight": None, "recovery_mode": "never"},
        "metrics": {"annual_sessions": 252, "risk_free_rate": 0, "cash_rate": 0, "definition_version": "1"},
    }
    BacktestSpec.model_validate(valid)
    with pytest.raises(ValueError):
        BacktestSpec.model_validate({**valid, "unused": True})
    with pytest.raises(ValueError):
        BacktestSpec.model_validate({**valid, "initial_cash": 0})
    with pytest.raises(ValueError):
        BacktestSpec.model_validate({**valid, "execution": {**valid["execution"], "slippage_rate": -0.01}})
    for bad in (
        {**valid, "metrics": {**valid["metrics"], "cash_rate": float("nan")}},
        {**valid, "risk": {**valid["risk"], "group_caps": {"industry": float("inf")}}},
        {**valid, "risk": {**valid["risk"], "max_stress_loss": 0.1}},
    ):
        with pytest.raises(ValueError):
            BacktestSpec.model_validate(bad)


def test_strategy_params_apply_schema_defaults_and_reject_unknown_keys():
    schema = {
        "type": "object", "properties": {
            "lookback": {"type": "integer", "minimum": 1, "default": 20},
            "method": {"type": "string", "enum": ["momentum", "value"], "default": "momentum"},
        },
    }
    assert validate_parameters(schema, {"lookback": 5}) == {"lookback": 5, "method": "momentum"}
    with pytest.raises(ValueError):
        validate_parameters(schema, {"lookback": 5, "extra": True})
    with pytest.raises(ValueError):
        validate_parameters(schema, {"lookback": 0})


@pytest.mark.parametrize("schema", [
    {"$defs": {"settings": {"type": "object", "properties": {"lookback": {"type": "integer", "default": 20}}}}, "type": "object", "properties": {"settings": {"$ref": "#/$defs/settings"}}},
    {"allOf": [{"type": "object", "properties": {"lookback": {"type": "integer", "default": 20}}}]},
    {"type": ["object", "null"], "properties": {"settings": {"type": "object"}}},
    {},
])
def test_unsupported_strategy_schema_composition_is_rejected(schema):
    with pytest.raises(ValueError):
        validate_parameters(schema, {"settings": {"surprise": True}})


def test_typeless_nested_object_schema_is_rejected_before_defaults_are_skipped():
    schema = {"type": "object", "properties": {"settings": {"properties": {"lookback": {"type": "integer", "default": 20}}}}}
    with pytest.raises(ValueError):
        validate_parameters(schema, {"settings": {"surprise": True}})
    with pytest.raises(ValueError):
        validate_parameters(schema, {"settings": {}})


def test_parameter_named_allof_is_not_treated_as_schema_composition():
    schema = {"type": "object", "properties": {"allOf": {"type": "integer", "default": 7}}}
    assert validate_parameters(schema, {}) == {"allOf": 7}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_strategy_parameters_must_be_finite_json_numbers(bad):
    schema = {"type": "object", "properties": {"weight": {"type": "number"}}}
    with pytest.raises(ValueError):
        validate_parameters(schema, {"weight": bad})
    with pytest.raises(ValueError):
        validate_parameters({"type": "object", "properties": {"weight": {"type": "number", "default": bad}}}, {})


def test_manifest_rejects_unknown_fields(tmp_path):
    path = dataset_case(tmp_path)
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["surprise"] = 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_dataset(path)


def test_manifest_rejects_nonfinite_universe_turnover(tmp_path):
    path = dataset_case(tmp_path)
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["universe_rule"]["min_avg_turnover_20d"] = float("nan")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_dataset(path)


def test_same_day_dividend_record_and_ex_date_is_rejected(tmp_path):
    path = dataset_case(tmp_path)
    pd.DataFrame([{"symbol": "000001", "event_type": "dividend", "record_date": "2024-01-02", "ex_date": "2024-01-02", "pay_date": "2024-01-03", "cash_per_share": 0.1, "split_ratio": 1}]).to_csv(path / "events.csv", index=False)
    with pytest.raises(ValueError):
        load_dataset(path)


def test_event_dates_must_be_covered_by_calendar(tmp_path):
    path = dataset_case(tmp_path)
    pd.DataFrame([{"symbol": "000001", "event_type": "dividend", "record_date": "2024-01-02", "ex_date": "2024-01-03", "pay_date": "2024-01-08", "cash_per_share": 0.1, "split_ratio": 1}]).to_csv(path / "events.csv", index=False)
    with pytest.raises(ValueError):
        load_dataset(path)


def test_adjusted_research_requires_disclosed_limitation(tmp_path):
    path = dataset_case(tmp_path, "adjusted_research")
    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["limitations"] = []
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_dataset(path)


def test_backtest_symbols_must_be_registered(tmp_path):
    bundle = load_dataset(dataset_case(tmp_path))
    spec = BacktestSpec.model_validate({
        "start": "2024-01-01", "end": "2024-01-05", "symbols": ["999999"], "initial_cash": 100_000, "seed": 1,
        "execution": {"commission_rate": 0, "minimum_commission": 0, "slippage_rate": 0, "max_stale_sessions": 0, "model": "next_open"},
        "risk": {"max_total_weight": 1, "max_symbol_weight": 1, "group_caps": {}, "stress_scenarios": [], "max_stress_loss": None, "drawdown_pause": None, "pause_action": "block_new", "pause_target_weight": None, "recovery_mode": "never"},
        "metrics": {"annual_sessions": 252, "risk_free_rate": 0, "cash_rate": 0, "definition_version": "1"},
    })
    with pytest.raises(ValueError):
        spec.validate_for_dataset(bundle)


def test_sample_data_publishes_loadable_demo_bundle(tmp_path):
    generate_sample_data(tmp_path, years=1)
    bundle = load_dataset(tmp_path)
    assert bundle.manifest.is_demo is True
    assert bundle.price_mode == "adjusted_research"
    assert bundle.calendar.shape[0] == 252
    assert set(bundle.prices["symbol"]) == set(bundle.instruments["symbol"])


def test_parquet_uses_same_validation_as_csv(tmp_path):
    path = dataset_case(tmp_path)
    prices = pd.read_csv(path / "prices.csv", dtype={"symbol": str})
    prices.loc[0, "close"] = -1
    prices.to_parquet(path / "prices.parquet", index=False)
    with pytest.raises(ValueError):
        load_dataset(path)
