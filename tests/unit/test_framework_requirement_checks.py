from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from quanto.backtest.dataset_identity import standard_table_semantics
from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.data.point_in_time import TableSemantics
from quanto.framework import (AssetBinding, AssetMapping, BenchmarkBinding, RequirementField,
                              RequirementItem, ResearchInterval)
from quanto.workbench.framework_capabilities import _stable_daily_at, _visible_at, check_requirements
from tests.fixtures.engine_cases import bundle_case, spec_case
from tests.fixtures.framework_cases import facts_bundle
from quanto.data.contracts import InstrumentId


def _case(days=5, symbols=("A", "B")):
    bundle = bundle_case(symbols=symbols, days=days)
    pairs = tuple(AssetMapping(instrument_id=InstrumentId(exchange="SSE", instrument_type="etf", code=s),
                               local_symbol=s, evidence_ref="fixture") for s in symbols)
    binding = AssetBinding(dataset_version_id="fixture", dataset_content_hash="a" * 64, assets=pairs,
                           benchmark=BenchmarkBinding(custom_series_id="benchmark", return_basis="price_index", evidence_ref="fixture"),
                           table_semantics=standard_table_semantics(bundle, "fixture"))
    spec = spec_case(bundle)
    interval = ResearchInterval(start=spec.start, end=spec.end, warmup_start=spec.start)
    return bundle, binding, pairs, spec, interval


def _requirement(table, interval, fields, *, required=True, missing_policy="reject", warmup_sessions=0):
    return RequirementItem(requirement_id=f"need-{table}", table=table, semantic_version="1",
                           fields=tuple(RequirementField(name=name, dtype=dtype, unit=unit) for name, dtype, unit in fields),
                           scope="basket", interval=interval, availability_rule="session_close" if table == "prices" else "timestamp",
                           price_modes=("raw_cash",), required=required, missing_policy=missing_policy,
                           warmup_sessions=warmup_sessions)


def _checks(bundle, binding, pairs, spec, requirements, semantics=None):
    return check_requirements(bundle, binding, pairs, requirements, ETF_TRADING_DEFINITION, spec,
                              table_semantics=semantics)


def _blocked(checks, code):
    return [item for item in checks if item.status == "blocked" and item.code == code]


def test_selected_subset_is_ready_and_does_not_expand_to_dataset_superset():
    bundle, binding, pairs, spec, interval = _case()
    selected = (pairs[1],)
    spec = spec.model_copy(update={"symbols": ["B"]})
    requirement = _requirement("prices", interval, (("close", "float", "price"),))
    assert not [item for item in _checks(bundle, binding, selected, spec, (requirement,)) if item.status == "blocked"]


def test_all_trading_tables_are_ready_with_synthetic_daily_facts():
    bundle, binding, pairs, spec, interval = _case()
    from quanto.framework import FrozenBasketRevision
    from datetime import datetime, timezone
    basket = FrozenBasketRevision(basket_id="fixture", revision=1,
        members=tuple(pair.instrument_id for pair in pairs),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), selection_note="fixture")
    requirements = ETF_TRADING_DEFINITION.resolve_requirements({}, basket, interval)
    assert not [item for item in _checks(bundle, binding, pairs, spec, requirements) if item.status == "blocked"]


def test_prelisting_etf_needs_no_price_or_status_but_listed_gaps_block():
    from datetime import datetime, timezone
    from quanto.framework import FrozenBasketRevision

    bundle, binding, pairs, spec, interval = _case()
    listed = pd.Timestamp("2024-01-03")
    instruments = bundle.instruments.copy()
    instruments.loc[instruments.symbol == "B", "listing_date"] = listed
    prices = bundle.prices.loc[~((bundle.prices.symbol == "B") & (bundle.prices.date < listed))]
    status = bundle.status.loc[~((bundle.status.symbol == "B") & (bundle.status.date < listed))]
    bundle = replace(bundle, instruments=instruments, prices=prices, status=status)
    basket = FrozenBasketRevision(basket_id="fixture", revision=1,
        members=tuple(pair.instrument_id for pair in pairs),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc), selection_note="fixture")
    requirements = ETF_TRADING_DEFINITION.resolve_requirements({}, basket, interval)
    assert not [item for item in _checks(bundle, binding, pairs, spec, requirements) if item.status == "blocked"]

    missing_price = replace(bundle, prices=prices.loc[~((prices.symbol == "B") & (prices.date == listed))])
    assert _blocked(_checks(missing_price, binding, pairs, spec, requirements), "DATA_REQUIREMENT_UNSATISFIED")
    missing_status = replace(bundle, status=status.loc[~((status.symbol == "B") & (status.date == listed))])
    assert _blocked(_checks(missing_status, binding, pairs, spec, requirements), "EXECUTION_CAPABILITY_MISSING")


def test_suspension_can_lack_price_but_still_needs_status():
    bundle, binding, pairs, spec, interval = _case()
    suspended = pd.Timestamp("2024-01-03")
    prices = bundle.prices.loc[~((bundle.prices.symbol == "B") & (bundle.prices.date == suspended))]
    status = bundle.status.copy()
    status.loc[(status.symbol == "B") & (status.date == suspended), ["status", "can_buy", "can_sell"]] = ["suspended", False, False]
    bundle = replace(bundle, prices=prices, status=status)
    requirement = _requirement("prices", interval, (("close", "float", "price"),))
    assert not [item for item in _checks(bundle, binding, pairs, spec, (requirement,)) if item.status == "blocked"]
    bundle = replace(bundle, status=status.loc[~((status.symbol == "B") & (status.date == suspended))])
    assert _blocked(_checks(bundle, binding, pairs, spec, (requirement,)), "EXECUTION_CAPABILITY_MISSING")


def test_missing_financial_table_is_blocked_but_optional_masked_table_is_allowed():
    bundle, binding, pairs, spec, interval = _case()
    requirement = _requirement("financials", interval, (("profit", "float", "CNY"),))
    assert _blocked(_checks(bundle, binding, pairs, spec, (requirement,)), "DATA_REQUIREMENT_UNSATISFIED")
    optional = requirement.model_copy(update={"required": False, "missing_policy": "mask"})
    assert not [item for item in _checks(bundle, binding, pairs, spec, (optional,)) if item.status == "blocked"]


def test_actual_observed_price_warmup_is_required_per_member():
    bundle, binding, pairs, spec, interval = _case(days=5)
    bundle = replace(bundle, prices=bundle.prices.loc[~((bundle.prices.symbol == "B") &
                               (bundle.prices.date == pd.Timestamp("2024-01-01")))])
    interval = interval.model_copy(update={"start": date(2024, 1, 3)})
    spec = spec.model_copy(update={"start": interval.start})
    requirement = _requirement("prices", interval, (("close", "float", "price"),), warmup_sessions=2)
    checks = _checks(bundle, binding, pairs, spec, (requirement,))
    assert any(item.field_path == "requirements[0].warmup_sessions" for item in _blocked(checks, "DATA_REQUIREMENT_UNSATISFIED"))


def test_existing_optional_data_with_wrong_unit_is_blocked():
    bundle, binding, semantics = facts_bundle()
    pairs = binding.assets
    spec = spec_case(bundle)
    interval = ResearchInterval(start=spec.start, end=spec.end, warmup_start=spec.start)
    requirement = _requirement("facts", interval, (("value", "float", "USD"),), required=False, missing_policy="mask")
    checks = _checks(bundle, binding, pairs, spec, (requirement,), semantics)
    assert any(item.field_path == "requirements[0].fields[0]" for item in _blocked(checks, "DATA_REQUIREMENT_UNSATISFIED"))


def test_optional_mask_does_not_accept_malformed_present_values():
    bundle, binding, pairs, spec, interval = _case()
    prices = bundle.prices.copy()
    prices["close"] = prices["close"].astype(object)
    prices.loc[0, "close"] = "invalid"
    bundle = replace(bundle, prices=prices)
    requirement = _requirement("prices", interval, (("close", "float", "price"),), required=False, missing_policy="mask")
    assert any(item.field_path == "requirements[0].fields[0]" for item in
               _blocked(_checks(bundle, binding, pairs, spec, (requirement,)), "DATA_REQUIREMENT_UNSATISFIED"))


def test_unregistered_auxiliary_reader_blocks_present_table():
    bundle, binding, semantics = facts_bundle()
    spec = spec_case(bundle)
    interval = ResearchInterval(start=spec.start, end=spec.end, warmup_start=spec.start)
    requirement = _requirement("facts", interval, (("value", "float", "score"),))
    assert _blocked(_checks(bundle, binding, binding.assets, spec, (requirement,)), "DEFINITION_UNAVAILABLE")


def test_future_and_ambiguous_auxiliary_facts_are_not_usable():
    bundle, binding, semantics = facts_bundle(rows=[dict(symbol="A", period_end="2023-12-31", revision_id="r1",
        revision_sequence=1, available_at="2024-01-09T08:00:00Z", value=10.)])
    pairs = (binding.assets[0],)
    spec = spec_case(bundle, symbols=["A"])
    interval = ResearchInterval(start=spec.start, end=spec.end, warmup_start=spec.start)
    requirement = _requirement("facts", interval, (("value", "float", "score"),))
    assert _blocked(_checks(bundle, binding, pairs, spec, (requirement,), semantics), "DATA_REQUIREMENT_UNSATISFIED")
    duplicate = pd.concat([bundle.auxiliary["facts"], bundle.auxiliary["facts"]], ignore_index=True)
    duplicate["available_at"] = "2024-01-02T06:00:00Z"
    bundle = replace(bundle, auxiliary={"facts": duplicate})
    assert _blocked(_checks(bundle, binding, pairs, spec, (requirement,), semantics), "TEMPORAL_EVIDENCE_MISSING")


def test_unknown_execution_event_is_blocked_even_with_adjusted_prices():
    bundle, binding, pairs, spec, interval = _case()
    events = pd.DataFrame([dict(symbol="A", event_type="delisting", record_date=pd.NaT,
                                ex_date=pd.Timestamp("2024-01-03"), pay_date=pd.NaT,
                                cash_per_share=None, split_ratio=None)])
    bundle = replace(bundle, events=events, manifest=bundle.manifest.model_copy(update={
        "price_mode": "adjusted_research", "event_coverage": "partial", "limitations": ["synthetic"]}))
    assert _blocked(_checks(bundle, binding, pairs, spec, ()), "EXECUTION_CAPABILITY_MISSING")


def test_split_event_allows_irrelevant_nullable_cash_columns():
    bundle, binding, pairs, spec, interval = _case()
    event = pd.DataFrame([dict(symbol="A", event_type="split", record_date=pd.NaT,
                               ex_date=pd.Timestamp("2024-01-03"), pay_date=pd.NaT,
                               cash_per_share=None, split_ratio=2.)])
    bundle = replace(bundle, events=event)
    requirement = RequirementItem(requirement_id="events", table="events", semantic_version="1",
        fields=(RequirementField(name="cash_per_share", dtype="float", unit="cash_per_share"),
                RequirementField(name="split_ratio", dtype="float", unit="ratio")),
        scope="basket", interval=interval, availability_rule="execution_event",
        price_modes=("raw_cash",), required=True, missing_policy="reject")
    assert not [item for item in _checks(bundle, binding, pairs, spec, (requirement,)) if item.status == "blocked"]


def test_check_is_repeatable_and_does_not_mutate_source_frames():
    bundle, binding, pairs, spec, interval = _case()
    requirement = _requirement("prices", interval, (("close", "float", "price"),))
    original = {name: getattr(bundle, name).copy(deep=True) for name in
                ("prices", "benchmark", "calendar", "instruments", "status", "events")}
    first = _checks(bundle, binding, pairs, spec, (requirement,))
    assert _checks(bundle, binding, pairs, spec, (requirement,)) == first
    for name, expected in original.items():
        pd.testing.assert_frame_equal(getattr(bundle, name), expected)


def test_stable_daily_fast_check_matches_generic_temporal_check():
    bundle, binding, pairs, spec, interval = _case()
    requirement = _requirement("prices", interval, (("close", "float", "price"),))
    prices = bundle.prices
    future = prices.loc[(prices.symbol == "A") & (prices.date == pd.Timestamp("2024-01-05"))].copy()
    future["date"] = pd.Timestamp("2024-01-08")
    invalid_future = future.copy()
    invalid_future["date"] = pd.Timestamp("2024-01-20")
    bad_value = future.copy()
    bad_value["close"] = "INVALID"
    missing_date = future.copy()
    missing_date["date"] = pd.NaT
    cases = (
        prices,
        prices.loc[~((prices.symbol == "B") & (prices.date == pd.Timestamp("2024-01-03")))],
        pd.concat((prices, prices.iloc[[0]]), ignore_index=True),
        pd.concat((prices, future, future), ignore_index=True),
        pd.concat((prices, invalid_future), ignore_index=True),
        pd.concat((prices, bad_value), ignore_index=True),
        pd.concat((prices, missing_date), ignore_index=True),
    )
    for frame in cases:
        fast = _checks(replace(bundle, prices=frame), binding, pairs, spec, (requirement,))
        generic = frame.copy()
        generic["date"] = generic["date"].astype(object)
        slow = _checks(replace(bundle, prices=generic), binding, pairs, spec, (requirement,))
        assert fast == slow
    undeclared = prices.assign(extra="INVALID")
    extra_requirement = _requirement("prices", interval, (("extra", "float", "score"),))
    generic = undeclared.copy()
    generic["date"] = generic["date"].astype(object)
    assert _checks(replace(bundle, prices=undeclared), binding, pairs, spec, (extra_requirement,)) == \
           _checks(replace(bundle, prices=generic), binding, pairs, spec, (extra_requirement,))
    source_ref_requirement = _requirement("prices", interval, (("source_ref", "string", "reference"),))
    generic = prices.copy()
    generic["date"] = generic["date"].astype(object)
    assert _checks(bundle, binding, pairs, spec, (source_ref_requirement,)) == \
           _checks(replace(bundle, prices=generic), binding, pairs, spec, (source_ref_requirement,))


def test_stable_daily_fast_check_rejects_visible_missing_business_key():
    bundle, binding, _, _, _ = _case()
    reader = TableSemantics.from_binding(next(item for item in binding.table_semantics if item.table == "prices"))
    frame = bundle.prices.copy()
    frame.loc[0, "symbol"] = None
    sessions = tuple(pd.to_datetime(bundle.calendar.date).dt.date)
    for check in (_stable_daily_at, lambda data, semantics, day, days:
                  _visible_at(data, semantics, day, days, binding.dataset_version_id)):
        with pytest.raises(ValueError, match="missing business identity"):
            check(frame, reader, sessions[-1], sessions)


def _known_fact(value=10., *, available_at="2024-01-01T06:00:00Z", revision_id="r1", revision_sequence=1):
    return dict(symbol="A", period_end="2023-12-31", revision_id=revision_id,
                revision_sequence=revision_sequence, available_at=available_at, value=value)


def _single_fact_case(rows, *, price_mode=None, start=None, warmup_sessions=0,
                      fields=(RequirementField(name="value", dtype="float", unit="score"),),
                      required=True, missing_policy="reject"):
    rules = {"price_mode": price_mode} if price_mode else None
    bundle, binding, semantics = facts_bundle(rows=rows, rule_changes=rules)
    spec = spec_case(bundle, symbols=["A"])
    if start:
        spec = spec.model_copy(update={"start": start})
    interval = ResearchInterval(start=spec.start, end=spec.end, warmup_start=spec.start)
    requirement = RequirementItem(requirement_id="facts", table="facts", semantic_version="1",
        fields=fields, scope="basket", interval=interval, warmup_sessions=warmup_sessions,
        availability_rule="timestamp", price_modes=("raw_cash",), required=required, missing_policy=missing_policy)
    return bundle, binding, (binding.assets[0],), spec, requirement, semantics


def test_auxiliary_without_price_basis_can_satisfy_facts_requirement():
    case = _single_fact_case([_known_fact()])
    assert not [check for check in _checks(*case[:4], (case[4],), case[5]) if check.status == "blocked"]


def test_masked_absent_field_does_not_hide_invalid_present_fact():
    fields = (RequirementField(name="absent", dtype="float", unit="score"),
              RequirementField(name="value", dtype="float", unit="score"))
    case = _single_fact_case([_known_fact("INVALID")], price_mode="raw_cash", fields=fields,
                             required=False, missing_policy="mask")
    checks = _checks(*case[:4], (case[4],), case[5])
    assert any(check.field_path == "requirements[0].fields[1]" for check in
               _blocked(checks, "DATA_REQUIREMENT_UNSATISFIED"))


def test_masked_absent_field_does_not_hide_ambiguous_revision():
    fields = (RequirementField(name="absent", dtype="float", unit="score"),
              RequirementField(name="value", dtype="float", unit="score"))
    case = _single_fact_case([_known_fact(), _known_fact()], price_mode="raw_cash", fields=fields,
                             required=False, missing_policy="mask")
    assert _blocked(_checks(*case[:4], (case[4],), case[5]), "TEMPORAL_EVIDENCE_MISSING")


def test_non_price_warmup_cannot_be_silently_ignored():
    case = _single_fact_case([_known_fact()], price_mode="raw_cash", warmup_sessions=999)
    checks = _checks(*case[:4], (case[4],), case[5])
    assert any(check.field_path == "requirements[0].warmup_sessions" and
               check.code in {"DEFINITION_UNAVAILABLE", "DATA_REQUIREMENT_UNSATISFIED"}
               for check in checks if check.status == "blocked")


def test_future_invalid_revision_does_not_invalidate_known_fact():
    case = _single_fact_case([_known_fact(), _known_fact("INVALID", available_at="2024-01-09T08:00:00Z",
                                                      revision_id="r2", revision_sequence=2)], price_mode="raw_cash")
    assert not [check for check in _checks(*case[:4], (case[4],), case[5]) if check.status == "blocked"]


def test_superseded_null_revision_before_start_does_not_invalidate_known_fact():
    case = _single_fact_case([_known_fact(None), _known_fact(10., available_at="2024-01-02T06:00:00Z",
                                                              revision_id="r2", revision_sequence=2)],
                             price_mode="raw_cash", start=date(2024, 1, 3))
    assert not [check for check in _checks(*case[:4], (case[4],), case[5]) if check.status == "blocked"]


def test_future_bad_price_outside_requirement_interval_is_ignored():
    bundle, binding, pairs, spec, interval = _case()
    future = bundle.prices.loc[(bundle.prices.symbol == "A") &
                               (bundle.prices.date == pd.Timestamp("2024-01-05"))].copy()
    future["date"] = pd.Timestamp("2024-01-08")
    future["close"] = "INVALID"
    bundle = replace(bundle, prices=pd.concat([bundle.prices, future], ignore_index=True))
    requirement = _requirement("prices", interval, (("close", "float", "price"),))
    assert not [check for check in _checks(bundle, binding, pairs, spec, (requirement,)) if check.status == "blocked"]
