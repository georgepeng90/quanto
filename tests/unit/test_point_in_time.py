from dataclasses import FrozenInstanceError, replace
from datetime import date

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from quanto.data.point_in_time import TableSemantics, _bind_point_in_time, session_cutoff
from quanto.framework import TableSemanticBinding
from tests.fixtures.engine_cases import bundle_case
from tests.fixtures.framework_cases import facts_bundle, tables, view_at


def test_future_revision_does_not_change_past_frame():
    bundle, binding, semantics = facts_bundle()
    first = view_at(bundle, binding, semantics).table("facts", ("A",))
    bundle.auxiliary["facts"].loc[1, "value"] = 999999.
    repeated = view_at(bundle, binding, semantics).table("facts", ("A",))
    assert_frame_equal(first, repeated)
    assert first.value.tolist() == [10.]
    assert view_at(bundle, binding, semantics, "2024-01-04").table("facts", ("A",)).value.tolist() == [10.]
    assert view_at(bundle, binding, semantics, "2024-01-05").table("facts", ("A",)).value.tolist() == [999999.]


def test_close_boundary_is_inclusive_and_after_close_disclosure_waits():
    bundle, binding, semantics = facts_bundle()
    bundle.auxiliary["facts"].loc[0, "available_at"] = "2024-01-02T07:00:00Z"
    before = view_at(bundle, binding, semantics, cutoff=pd.Timestamp("2024-01-02T06:59:59Z").to_pydatetime())
    at = view_at(bundle, binding, semantics, "2024-01-02")
    assert before.table("facts", ("A",)).empty
    assert at.table("facts", ("A",)).value.tolist() == [10.]


def test_daily_bar_visibility_is_not_disclosure_date_rule():
    bundle, binding, semantics = facts_bundle()
    view = view_at(bundle, binding, semantics, "2024-01-03")
    bars = view.bars(("A",), date(2024, 1, 1))
    assert bars.date.max().date() == date(2024, 1, 3)
    assert (bars.source_available_at <= view.signal_cutoff).all()
    bundle.prices.loc[bundle.prices.date.dt.day == 4, "open"] = 777.
    assert 777. not in view_at(bundle, binding, semantics, "2024-01-03").bars(("A",), date(2024, 1, 1)).open.values


def test_date_only_disclosure_needs_opt_in_and_next_trading_close():
    bundle, binding, semantics = facts_bundle([dict(symbol="A", period_end="2023-12-31", available_at="2024-01-05", value=10.)],
        dict(availability_rule="next_session_date", revision_policy="none", revision_id_column=None, revision_order_column=None))
    bundle = replace(bundle, calendar=pd.DataFrame({"date": pd.bdate_range("2024-01-01", "2024-01-09")}))
    assert view_at(bundle, binding, semantics, "2024-01-05").table("facts", ("A",)).empty
    before = view_at(bundle, binding, semantics, cutoff=pd.Timestamp("2024-01-08T06:59:59Z").to_pydatetime())
    assert before.table("facts", ("A",)).empty
    frame = view_at(bundle, binding, semantics, "2024-01-08").table("facts", ("A",))
    assert frame.source_available_at.iloc[0] == pd.Timestamp("2024-01-08T07:00:00Z")
    bundle2, binding2, semantics2 = facts_bundle()
    bundle2.auxiliary["facts"].loc[0, "available_at"] = "2024-01-02"
    with pytest.raises(ValueError, match="TEMPORAL_EVIDENCE_MISSING"):
        view_at(bundle2, binding2, semantics2)


def test_filter_visibility_then_select_revision_then_effective_interval():
    rows = [dict(symbol="A", rule="lot", revision_id="r1", revision_sequence=1, available_at="2024-01-01T00:00:00Z",
                 effective_from="2024-01-02T00:00:00+08:00", effective_to="2024-01-04T00:00:00+08:00", value=100.),
            dict(symbol="A", rule="lot", revision_id="r2", revision_sequence=2, available_at="2024-01-05T00:00:00Z",
                 effective_from="2024-01-04T00:00:00+08:00", effective_to=None, value=200.)]
    bundle, binding, semantics = facts_bundle(rows, dict(business_keys=["symbol", "rule"], effective_from_column="effective_from", effective_to_column="effective_to"))
    assert view_at(bundle, binding, semantics, "2024-01-01").table("facts", ("A",)).empty
    assert view_at(bundle, binding, semantics, "2024-01-02").table("facts", ("A",)).value.tolist() == [100.]
    assert view_at(bundle, binding, semantics, "2024-01-04").table("facts", ("A",)).empty
    assert view_at(bundle, binding, semantics, "2024-01-05").table("facts", ("A",)).value.tolist() == [200.]


@pytest.mark.parametrize("mutation", ["tie", "unknown_order", "naive", "downloaded_only"])
def test_ambiguous_or_unproven_temporal_records_are_rejected(mutation):
    bundle, binding, semantics = facts_bundle()
    frame = bundle.auxiliary["facts"]
    if mutation == "tie":
        frame.loc[1, ["available_at", "revision_sequence"]] = ["2024-01-02T00:00:00Z", 1]
    elif mutation == "unknown_order":
        frame.loc[1, ["available_at", "revision_sequence"]] = ["2024-01-02T00:00:00Z", None]
    elif mutation == "naive":
        frame.loc[0, "available_at"] = "2024-01-02T06:59:00"
    else:
        frame.rename(columns={"available_at": "downloaded_at"}, inplace=True)
    with pytest.raises(ValueError, match="TEMPORAL_EVIDENCE_MISSING"):
        view_at(bundle, binding, semantics)


def test_unordered_duplicate_facts_are_not_resolved_by_row_order():
    bundle, binding, semantics = facts_bundle(rule_changes=dict(revision_policy="none", revision_id_column=None, revision_order_column=None))
    bundle.auxiliary["facts"].loc[1, "available_at"] = "2024-01-02T00:00:00Z"
    with pytest.raises(ValueError, match="TEMPORAL_EVIDENCE_MISSING"):
        view_at(bundle, binding, semantics)


def test_view_copies_are_isolated_and_cannot_expand_cutoff_or_asset_space():
    bundle, binding, semantics = facts_bundle()
    view = view_at(bundle, binding, semantics)
    initial = view.bars(("A",), date(2024, 1, 1))
    returned = view.bars(("A",), date(2024, 1, 1))
    returned.loc[:, "close"] = 1234.
    bundle.prices.loc[:, "close"] = 9999.
    assert_frame_equal(view.bars(("A",), date(2024, 1, 1)), initial)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        view.signal_cutoff = session_cutoff(date(2030, 1, 1))
    with pytest.raises(TypeError):
        view.bars(("A",), date(2024, 1, 1), end=date(2030, 1, 1))
    with pytest.raises(ValueError, match="basket"):
        view.table("facts", ("OUTSIDE",))
    assert view.ordered_symbols == ("B", "A")


def test_unknown_semantics_version_cannot_fall_back_to_current_rule():
    bundle, binding, semantics = facts_bundle()
    old = binding.table_semantics[0]
    unknown = TableSemanticBinding(**{**old.model_dump(exclude={"declaration_hash"}), "version": "future"})
    with pytest.raises(ValueError, match="DEFINITION_UNAVAILABLE"):
        TableSemantics.from_binding(unknown)
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        _bind_point_in_time(tables(bundle), binding, ("A",), session_cutoff(date(2024, 1, 3)), semantics=semantics[:-1])


def test_execution_event_and_undated_static_facts_are_not_disclosure_evidence():
    bundle, binding, semantics = facts_bundle()
    view = view_at(bundle, binding, semantics)
    for table in ("events", "instruments"):
        with pytest.raises(ValueError, match="TEMPORAL_EVIDENCE_MISSING"):
            view.table(table, ("A",))


@pytest.mark.parametrize("raw", ["2024-01-02T14:00:00", "2024-01-02 00:00:00"])
@pytest.mark.parametrize("for_run", [False, True])
def test_dataset_loader_does_not_invent_timezone_for_naive_disclosure(tmp_path, raw, for_run):
    import json
    from quanto.backtest.dataset_loaders import load_dataset, load_dataset_for_run
    from tests.fixtures.engine_cases import spec_case
    from tests.fixtures.backtest_cases import dataset_case
    root = dataset_case(tmp_path / "source")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["auxiliary_tables"] = {"facts": "facts.csv"}
    (root / "manifest.json").write_text(json.dumps(manifest))
    pd.DataFrame(dict(available_at=[raw], value=[1])).to_csv(root / "facts.csv", index=False)
    with pytest.raises(ValueError, match="TEMPORAL_EVIDENCE_MISSING"):
        if for_run:
            load_dataset_for_run(root, spec_case(bundle_case(symbols=("000001",), days=3)))
        else:
            load_dataset(root)


@pytest.mark.parametrize("for_run", [False, True])
def test_dataset_loader_preserves_date_only_disclosure_as_a_date(tmp_path, for_run):
    import json
    from quanto.backtest.dataset_loaders import load_dataset, load_dataset_for_run
    from tests.fixtures.engine_cases import spec_case
    from tests.fixtures.backtest_cases import dataset_case
    root = dataset_case(tmp_path / "source")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["auxiliary_tables"] = {"facts": "facts.csv"}
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "facts.csv").write_text("available_at,value\n2024-01-02,1\n")
    bundle = load_dataset_for_run(root, spec_case(bundle_case(symbols=("000001",), days=3))) if for_run else load_dataset(root)
    assert bundle.auxiliary["facts"].available_at.iloc[0] == "2024-01-02"


def test_overlapping_effective_rules_are_not_exposed_as_two_current_values():
    rows = [dict(symbol="A", rule="lot", segment="one", available_at="2024-01-01T00:00:00Z",
                 effective_from="2024-01-01T00:00:00Z", effective_to="2024-01-04T00:00:00Z", value=100.),
            dict(symbol="A", rule="lot", segment="two", available_at="2024-01-01T00:00:00Z",
                 effective_from="2024-01-02T00:00:00Z", effective_to=None, value=200.)]
    bundle, binding, semantics = facts_bundle(rows, dict(business_keys=["symbol", "rule", "segment"],
        effective_entity_keys=["symbol", "rule"], revision_policy="none", revision_id_column=None, revision_order_column=None,
        effective_from_column="effective_from", effective_to_column="effective_to"))
    with pytest.raises(ValueError, match="TEMPORAL_EVIDENCE_MISSING"):
        view_at(bundle, binding, semantics)


def test_reader_cannot_override_the_bound_time_rules():
    bundle, binding, semantics = facts_bundle()
    last = semantics[-1]
    from quanto.data.point_in_time import TemporalRules
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        TableSemantics(binding=last.binding, rules=TemporalRules(**{**last.rules.model_dump(), "revision_policy": "none"}))


def test_view_freezes_caller_owned_basket_list():
    bundle, binding, semantics = facts_bundle()
    symbols = ["A"]
    view = _bind_point_in_time(tables(bundle), binding, symbols, session_cutoff(date(2024, 1, 3)), semantics=semantics)
    symbols.append("B")
    assert view.ordered_symbols == ("A",)
    with pytest.raises(ValueError, match="basket"):
        view.bars(("B",), date(2024, 1, 1))
