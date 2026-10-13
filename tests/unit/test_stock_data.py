from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.stock_data import validate_stock_data
from quanto.framework import ResearchInterval
from tests.fixtures.stock_training_cases import stock_case, stock_binding


@pytest.fixture
def case(tmp_path):
    stock_case(tmp_path)
    bundle = load_dataset(tmp_path)
    return bundle, stock_binding(bundle), ("000001",), ResearchInterval(start=date(2024, 1, 15), end=date(2024, 2, 23), warmup_start=date(2024, 1, 1))


def test_valid_stock_context_is_immutable_and_uses_local_identity(case):
    context = validate_stock_data(*case)
    assert context.fee_rule("000001", date(2024, 1, 15), "sell")["stamp_duty_rate"] == .0005
    assert context.instruments["000001"]["instrument_id"] == "SZSE:stock:000001"
    with pytest.raises(TypeError):
        context.instruments["000001"]["board"] = "other"


def test_stock_evidence_readers_are_registered(case):
    from quanto.data.point_in_time import TableSemantics
    for declaration in case[1].table_semantics:
        assert TableSemantics.from_binding(declaration).binding == declaration


def test_capability_checks_reject_missing_stock_evidence(case):
    from quanto.backtest.trading import ETF_TRADING_DEFINITION
    from quanto.workbench.framework_capabilities import check_requirements
    from tests.fixtures.engine_cases import spec_case
    bundle, binding, symbols, interval = case
    spec = spec_case(bundle).model_copy(update={"symbols": list(symbols), "start": interval.start, "end": interval.end})
    del bundle.auxiliary["stock_sources"]
    definition = replace(ETF_TRADING_DEFINITION, supported_instrument_types=("stock",))
    checks = check_requirements(bundle, binding, binding.assets, (), definition, spec)
    assert any(check.name == "stock_execution_evidence" and check.code == "DATA_REQUIREMENT_UNSATISFIED" for check in checks)


@pytest.mark.parametrize("fault", ["overlap", "gap", "end_boundary", "negative", "nan", "buy_tax", "late"])
def test_fee_rules_require_unique_covered_intervals(case, fault):
    bundle, *args = case
    rules = bundle.auxiliary["fee_rules"]
    if fault == "overlap":
        bundle.auxiliary["fee_rules"] = pd.concat([rules, rules.iloc[[0]]], ignore_index=True)
    elif fault in {"gap", "end_boundary"}:
        rules.loc[0, "effective_to"] = "2024-01-16" if fault == "gap" else "2024-01-15"
    elif fault in {"negative", "nan"}:
        rules.loc[0, "transfer_fee_rate"] = -1 if fault == "negative" else float("nan")
    elif fault == "buy_tax":
        rules.loc[0, "stamp_duty_rate"] = .001
    else:
        rules.loc[0, "available_at"] = pd.Timestamp("2024-01-15T16:00:00+08:00")
    with pytest.raises(ValueError, match="DATA_REQUIREMENT_UNSATISFIED"):
        validate_stock_data(bundle, *args)


def test_fee_rules_switch_on_exclusive_end(case):
    bundle, *args = case
    rules = bundle.auxiliary["fee_rules"]
    extra = rules.copy()
    rules["effective_to"] = "2024-02-01"
    extra["effective_from"] = "2024-02-01"
    extra["transfer_fee_rate"] = .00002
    bundle.auxiliary["fee_rules"] = pd.concat([rules, extra], ignore_index=True)
    context = validate_stock_data(bundle, *args)
    assert context.fee_rule("000001", date(2024, 1, 31), "buy")["transfer_fee_rate"] == .00001
    assert context.fee_rule("000001", date(2024, 2, 1), "buy")["transfer_fee_rate"] == .00002


@pytest.mark.parametrize("fault", ["late", "missing", "duplicate", "unknown_source", "string_flag", "suspended_buy"])
def test_open_status_rejects_late_or_missing_evidence(case, fault):
    bundle, *args = case
    frame = bundle.auxiliary["stock_open_status"]
    index = 10
    if fault == "missing":
        bundle.auxiliary["stock_open_status"] = frame.drop(index)
    elif fault == "duplicate":
        bundle.auxiliary["stock_open_status"] = pd.concat([frame, frame.iloc[[index]]], ignore_index=True)
    elif fault == "late":
        frame.loc[index, "available_at"] = pd.Timestamp("2024-01-15T15:00:00+08:00")
    elif fault == "unknown_source":
        frame.loc[index, "source_ref"] = "missing"
    elif fault == "string_flag":
        frame["can_buy"] = frame.can_buy.astype(object)
        frame.loc[index, "can_buy"] = "false"
    else:
        frame.loc[index, "status"] = "suspended"
    with pytest.raises(ValueError, match="DATA_REQUIREMENT_UNSATISFIED"):
        validate_stock_data(bundle, *args)


@pytest.mark.parametrize("fault", ["board", "lot", "settlement", "unit", "event_source", "coverage", "digest", "future_source", "unknown_event", "missing_table", "etf"])
def test_stock_identity_board_units_and_event_coverage_are_required(case, fault):
    bundle, binding, symbols, interval = case
    if fault in {"board", "lot", "settlement"}:
        column, value = {"board": ("board", "star"), "lot": ("lot_size", 10), "settlement": ("settlement_sessions", 0)}[fault]
        bundle.instruments.loc[0, column] = value
    elif fault == "unit":
        bundle = replace(bundle, manifest=bundle.manifest.model_copy(update={"volume_unit": "lot"}))
    elif fault == "event_source":
        bundle.manifest.field_semantics.clear()
    elif fault == "coverage":
        bundle.auxiliary["stock_sources"].loc[0, "coverage_end"] = "2024-01-30"
    elif fault == "digest":
        bundle.auxiliary["stock_sources"].loc[0, "payload_sha256"] = "0" * 64
    elif fault == "future_source":
        bundle.auxiliary["stock_sources"].loc[0, "published_at"] = "2024-12-31T00:00:00Z"
    elif fault == "unknown_event":
        bundle = replace(bundle, events=pd.DataFrame([dict(symbol="000001", event_type="delist")]))
    elif fault == "missing_table":
        del bundle.auxiliary["stock_sources"]
    else:
        asset = binding.assets[0]
        asset = asset.model_copy(update={"instrument_id": asset.instrument_id.model_copy(update={"instrument_type": "etf"})})
        binding = binding.model_copy(update={"assets": (asset,)})
    with pytest.raises(ValueError, match="DATA_REQUIREMENT_UNSATISFIED"):
        validate_stock_data(bundle, binding, symbols, interval)
