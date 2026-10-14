from dataclasses import replace
from datetime import date, datetime, timezone
from types import MappingProxyType

import pytest
import numpy as np

from quanto.factors.features import (
    FeatureCache,
    FeatureCell,
    FeatureField,
    FeatureSetDefinition,
    compute_fit_frame,
    compute_features,
    fit_transform,
    make_cell,
)
from quanto.data.contracts import InstrumentId
from quanto.framework import DefinitionRef, FrozenBasketRevision, ResearchInterval


T1 = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)
T2 = datetime(2024, 1, 3, 15, tzinfo=timezone.utc)
T3 = datetime(2024, 1, 4, 15, tzinfo=timezone.utc)


class View:
    def __init__(self, *, cutoff=T2, symbols=("A", "B"), binding="binding", dataset="dataset"):
        self.asof = cutoff.date()
        self.signal_cutoff = cutoff
        self.ordered_symbols = symbols
        self.binding_hash = binding
        self.dataset_content_hash = dataset
        self._feature_frames = {}

    def bars(self, symbols, start):
        raise AssertionError("This definition does not need bars")

    def table(self, name, symbols):
        raise AssertionError("This definition does not need tables")


def definition(compute, *, fields=None, fit=None, apply=None, implementation_hash="a" * 64):
    return FeatureSetDefinition(
        ref=DefinitionRef(kind="feature", id="example", version="1"),
        implementation_hash=implementation_hash,
        supported_instrument_types=("etf",),
        parameter_schema={"type": "object", "properties": {"shift": {"type": "number", "default": 0}}},
        resolve_requirements=lambda params, basket, interval: (),
        fields=fields or (FeatureField("price", "close", "CNY", "per_symbol", "days", "mask"),),
        compute=compute,
        fit=fit,
        apply=apply,
        transform_recipe="standardize-v1" if fit else "fixed-v1",
    )


def test_frame_keeps_order_and_masks_missing_symbol():
    def raw(view, params):
        return {"A": {"price": make_cell(12 + params["shift"], ((T1, "bar:A"),))}}, {}

    frame = compute_features(definition(raw), View(), {"shift": 1}, None)
    assert frame.ordered_symbols == ("A", "B")
    assert list(frame.per_symbol) == ["A", "B"]
    assert list(frame.per_symbol["B"]) == ["price"]
    assert frame.per_symbol["A"]["price"].value == 13
    assert frame.per_symbol["A"]["price"].age == 1
    assert frame.per_symbol["B"]["price"].value is None
    assert frame.per_symbol["B"]["price"].is_valid is False
    assert frame.per_symbol["B"]["price"].missing_reason == "missing"


def test_global_fields_are_separate_and_deeply_read_only():
    fields = (
        FeatureField("price", "close", "CNY", "per_symbol", "days", "mask"),
        FeatureField("breadth", "positive share", "ratio", "global", "days", "mask"),
    )
    frame = compute_features(
        definition(lambda view, params: ({}, {"breadth": make_cell(0.5, ((T1, "breadth"),))}), fields=fields),
        View(), {}, None,
    )
    assert list(frame.global_values) == ["breadth"]
    assert list(frame.per_symbol["A"]) == ["price"]
    with pytest.raises(TypeError):
        frame.per_symbol["A"]["price"] = FeatureCell(None, False, None, None, "changed")
    with pytest.raises(TypeError):
        frame.global_values["breadth"] = frame.per_symbol["A"]["price"]


def test_reject_policy_and_invalid_values_are_never_silently_zeroed():
    reject = (FeatureField("price", "close", "CNY", "per_symbol", "days", "reject"),)
    with pytest.raises(ValueError, match="price"):
        compute_features(definition(lambda view, params: ({}, {}), fields=reject), View(), {}, None)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="finite"):
            make_cell(bad, ((T1, "bar"),))
    with pytest.raises(ValueError, match="source"):
        FeatureCell(1.0, True, None, None, None)


def test_future_source_and_unsourced_value_are_rejected():
    for sources in (((T3, "future"),), ()):
        with pytest.raises(ValueError, match="source|cutoff"):
            compute_features(
                definition(lambda view, params: ({"A": {"price": make_cell(1, sources)}}, {})),
                View(), {}, None,
            )


def test_combined_cell_uses_latest_actual_source():
    combined = make_cell(7, ((T1, "bar"), (T2, "announcement")))
    frame = compute_features(
        definition(lambda view, params: ({"A": {"price": combined}}, {})), View(), {}, None,
    )
    cell = frame.per_symbol["A"]["price"]
    assert cell.source_available_at == T2
    assert cell.source_refs == ("bar", "announcement")
    assert cell.age == 0


def test_numpy_numeric_cells_serialize_to_a_stable_frame():
    frame = compute_features(
        definition(lambda view, params: ({"A": {"price": make_cell(np.float32(7.5), ((T1, "bar"),))}}, {})),
        View(), {}, None,
    )
    assert frame.per_symbol["A"]["price"].value == 7.5
    assert len(frame.frame_hash) == 64


def test_stale_value_is_masked_with_age_or_rejected_by_field_policy():
    masked_field = (FeatureField("price", "close", "CNY", "per_symbol", "days", "mask", max_age=0.5),)
    raw = lambda view, params: ({"A": {"price": make_cell(7, ((T1, "old-bar"),))}}, {})
    masked = compute_features(definition(raw, fields=masked_field), View(), {}, None).per_symbol["A"]["price"]
    assert masked.value is None
    assert masked.age == 1
    assert masked.missing_reason == "stale"
    reject_field = (FeatureField("price", "close", "CNY", "per_symbol", "days", "reject", max_age=0.5),)
    with pytest.raises(ValueError, match="max_age"):
        compute_features(definition(raw, fields=reject_field), View(), {}, None)


def test_output_shape_and_parameters_are_strict():
    with pytest.raises(ValueError, match="unknown|symbol|field"):
        compute_features(definition(lambda view, params: ({"C": {}}, {})), View(), {}, None)
    with pytest.raises(ValueError, match="unknown|Additional properties"):
        compute_features(definition(lambda view, params: ({}, {})), View(), {"undeclared": 1}, None)


def test_definition_schema_and_effective_parameters_are_recursively_read_only():
    seen = []

    def raw(view, params):
        seen.append(params)
        return {}, {}

    schema = {"type": "object", "properties": {"options": {"type": "object", "properties": {
        "weights": {"type": "array", "items": {"type": "number"}, "default": [1, 2]},
    }}}}
    contract = FeatureSetDefinition(
        ref=DefinitionRef(kind="feature", id="nested", version="1"),
        implementation_hash="c" * 64, supported_instrument_types=("etf",), parameter_schema=schema,
        resolve_requirements=lambda params, basket, interval: (),
        fields=(FeatureField("price", "close", "CNY", "per_symbol", "days", "mask"),),
        compute=raw,
    )
    schema["properties"]["options"]["properties"]["weights"]["default"].append(3)
    compute_features(contract, View(), {"options": {}}, None)
    assert seen[0]["options"]["weights"] == (1, 2)
    with pytest.raises(TypeError):
        seen[0]["options"]["weights"][0] = 9
    with pytest.raises(TypeError):
        contract.parameter_schema["properties"]["options"]["properties"]["weights"]["default"][0] = 9


def test_cache_separates_binding_transform_cutoff_order_and_params():
    calls = []

    def raw(view, params):
        calls.append(1)
        return {}, {}

    contract = definition(raw)
    cache = FeatureCache()
    base = View()
    first = cache.get_or_compute(contract, base, {}, None)
    assert cache.get_or_compute(contract, base, {}, None) is first
    variants = (
        (contract, View(binding="other"), {}, None),
        (contract, View(dataset="other"), {}, None),
        (contract, View(cutoff=T3), {}, None),
        (contract, View(symbols=("B", "A")), {}, None),
        (contract, base, {"shift": 1}, None),
        (definition(raw, implementation_hash="b" * 64), base, {}, None),
    )
    for item in variants:
        assert cache.get_or_compute(*item) is not first
    assert len(calls) == 7


def test_feature_frames_are_reused_by_new_caches_for_the_same_view():
    calls = []

    def raw(view, params):
        calls.append(params["shift"])
        return {"A": {"price": make_cell(12 + params["shift"], ((T1, "bar:A"),))}}, {}

    contract, view = definition(raw), View()
    first = FeatureCache().get_or_compute(contract, view, {}, None)
    repeated = FeatureCache().get_or_compute(contract, view, {}, None)
    shifted = FeatureCache().get_or_compute(contract, view, {"shift": 1}, None)
    assert repeated is first
    assert shifted.per_symbol["A"]["price"].value == 13
    assert calls == [0, 1]


def test_view_feature_memo_does_not_share_results_between_distinct_views():
    calls = []

    def raw(view, params):
        calls.append(view)
        return {}, {}

    contract = definition(raw)
    left, right = View(), View()
    first = FeatureCache().get_or_compute(contract, left, {}, None)
    second = FeatureCache().get_or_compute(contract, right, {}, None)
    assert second is not first
    assert len(calls) == 2
    assert first == second


def test_view_feature_memo_only_keeps_successfully_validated_frames():
    calls = []

    def raw(view, params):
        calls.append(1)
        moment = T3 if len(calls) == 1 else T1
        return {"A": {"price": make_cell(12, ((moment, "bar:A"),))}}, {}

    contract, view = definition(raw), View()
    with pytest.raises(ValueError, match="cutoff"):
        FeatureCache().get_or_compute(contract, view, {}, None)
    valid = FeatureCache().get_or_compute(contract, view, {}, None)
    assert FeatureCache().get_or_compute(contract, view, {}, None) is valid
    assert len(calls) == 2


def test_compute_never_fits_and_frozen_transform_is_stable():
    fit_calls = []

    def fit(frames, params):
        fit_calls.append(1)
        return {"scale": [2.0]}

    def apply(frame, artifact, params):
        assert artifact.data["scale"] == (2.0,)
        return frame

    contract = definition(lambda view, params: ({"A": {"price": make_cell(2, ((T1, "bar"),))}}, {}), fit=fit, apply=apply)
    with pytest.raises(ValueError, match="transform"):
        compute_features(contract, View(), {}, None)
    assert fit_calls == []
    training = compute_fit_frame(contract, View(cutoff=T1), {})
    training_later = compute_fit_frame(contract, View(cutoff=T2), {})
    assert fit_calls == []
    artifact = fit_transform(contract, (training, training_later), {})
    assert fit_calls == [1]
    assert artifact.training_start == date(2024, 1, 2)
    assert artifact.training_end == date(2024, 1, 3)
    assert artifact.training_frame_hashes == (training.frame_hash, training_later.frame_hash)
    assert artifact.training_dataset_hashes == ("dataset", "dataset")
    assert artifact.training_binding_hashes == ("binding", "binding")
    assert isinstance(artifact.data, MappingProxyType)
    with pytest.raises(TypeError):
        artifact.data["scale"] = (3.0,)
    a = compute_features(contract, View(), {}, artifact)
    b = compute_features(contract, View(), {}, artifact)
    assert a == b
    assert fit_calls == [1]
    with pytest.raises(ValueError, match="definition|parameter"):
        compute_features(definition(lambda view, params: ({}, {}), fit=fit, apply=apply, implementation_hash="d" * 64), View(), {}, artifact)
    replay = compute_features(contract, View(cutoff=T1), {}, artifact)
    assert replay.per_symbol["A"]["price"].value == 2
    assert fit_calls == [1]


def test_cache_distinguishes_fitted_artifacts_without_refitting():
    calls = []

    def raw(view, params):
        calls.append(1)
        return {}, {}

    contract = definition(raw, fit=lambda frames, params: {"factor": [frames[0].signal_cutoff.isoformat()]},
                          apply=lambda frame, artifact, params: frame)
    training_a = compute_fit_frame(contract, View(cutoff=T1), {})
    training_b = compute_fit_frame(contract, View(cutoff=T2), {})
    artifact_a = fit_transform(contract, (training_a,), {})
    artifact_b = fit_transform(contract, (training_b,), {})
    cache = FeatureCache()
    a = cache.get_or_compute(contract, View(cutoff=T3), {}, artifact_a)
    b = cache.get_or_compute(contract, View(cutoff=T3), {}, artifact_b)
    assert a.transform_hash != b.transform_hash
    assert a is not b
    assert cache.get_or_compute(contract, View(cutoff=T3), {}, artifact_a) is a


def test_fit_rejects_raw_frame_from_a_different_implementation():
    fitted = definition(lambda view, params: ({}, {}), fit=lambda frames, params: {},
                        apply=lambda frame, artifact, params: frame, implementation_hash="e" * 64)
    other = definition(lambda view, params: ({}, {}), fit=lambda frames, params: {},
                       apply=lambda frame, artifact, params: frame, implementation_hash="f" * 64)
    foreign = compute_fit_frame(other, View(cutoff=T1), {})
    with pytest.raises(ValueError, match="definition|identity"):
        fit_transform(fitted, (foreign,), {})


def test_fit_accepts_only_raw_frame_with_matching_schema_and_recipe():
    contract = definition(lambda view, params: ({}, {}), fit=lambda frames, params: {},
                          apply=lambda frame, artifact, params: frame)
    raw = compute_fit_frame(contract, View(cutoff=T1), {})
    artifact = fit_transform(contract, (raw,), {})
    transformed = compute_features(contract, View(cutoff=T2), {}, artifact)
    with pytest.raises(ValueError, match="raw fit"):
        fit_transform(contract, (transformed,), {})
    changed_field = FeatureField("price", "close", "USD", "per_symbol", "days", "mask")
    forged_schema = replace(raw, field_schema=(changed_field,))
    with pytest.raises(ValueError, match="field|schema"):
        fit_transform(contract, (forged_schema,), {})
    for forged in (replace(artifact, recipe="other"),
                   replace(artifact, definition_ref=DefinitionRef(kind="feature", id="other", version="1"))):
        with pytest.raises(ValueError, match="identity"):
            compute_features(contract, View(cutoff=T2), {}, forged)
    cache = FeatureCache()
    cache.get_or_compute(contract, View(cutoff=T2), {}, artifact)
    with pytest.raises(ValueError, match="identity"):
        cache.get_or_compute(contract, View(cutoff=T2), {},
                             replace(artifact, definition_ref=DefinitionRef(kind="feature", id="other", version="1")))


def test_supported_instrument_types_are_part_of_definition_identity():
    base = definition(lambda view, params: ({}, {}))
    both = replace(base, supported_instrument_types=("etf", "stock"))
    assert base.definition_hash != both.definition_hash


def test_resolver_receives_ordered_basket_and_exact_warmup_interval():
    basket = FrozenBasketRevision(
        basket_id="b", revision=1,
        members=(InstrumentId(exchange="SH", instrument_type="etf", code="510300"),
                 InstrumentId(exchange="SZ", instrument_type="etf", code="159915")),
        created_at=T1, selection_note="fixed",
    )
    interval = ResearchInterval(start=date(2024, 2, 1), end=date(2024, 2, 29),
                                warmup_start=date(2023, 11, 1))
    received = []

    def resolve(params, supplied_basket, supplied_interval):
        received.append((params["shift"], tuple(item.code for item in supplied_basket.members),
                         supplied_interval.warmup_start, supplied_interval.start, supplied_interval.end))
        return ()

    contract = replace(definition(lambda view, params: ({}, {})), resolve_requirements=resolve)
    assert contract.resolve_requirements({"shift": 2}, basket, interval) == ()
    assert received == [(2, ("510300", "159915"), date(2023, 11, 1), date(2024, 2, 1), date(2024, 2, 29))]


def test_carry_requires_explicit_permission_but_old_sources_are_not_automatically_carry():
    old_value = lambda view, params: ({"A": {"price": make_cell(7, ((T1, "window"),))}}, {})
    historic = compute_features(definition(old_value), View(), {}, None)
    assert historic.per_symbol["A"]["price"].value == 7
    assert historic.per_symbol["A"]["price"].age == 1
    same_session = datetime(2024, 1, 3, 14, tzinfo=timezone.utc)
    disclosed = compute_features(
        definition(lambda view, params: ({"A": {"price": make_cell(8, ((same_session, "announcement"),))}}, {})),
        View(), {}, None,
    )
    assert disclosed.per_symbol["A"]["price"].value == 8
    assert disclosed.per_symbol["A"]["price"].is_carried is False
    with pytest.raises(ValueError, match="carry"):
        compute_features(definition(lambda view, params: ({"A": {"price": make_cell(7, ((T1, "quarter"),), is_carried=True)}}, {})),
                         View(), {}, None)
    allowed = FeatureField("price", "published value", "CNY", "per_symbol", "days", "mask",
                           max_age=1, allow_carry_forward=True)
    contract = definition(lambda view, params: ({"A": {"price": make_cell(7, ((T1, "quarter"),), is_carried=True)}}, {}),
                          fields=(allowed,))
    carried = compute_features(contract, View(), {}, None).per_symbol["A"]["price"]
    assert carried.value == 7
    assert carried.is_carried is True
    assert carried.age == 1
    stale = compute_features(contract, View(cutoff=T3), {}, None).per_symbol["A"]["price"]
    assert stale.value is None
    assert stale.missing_reason == "stale"
    assert stale.age == 2
    assert stale.is_carried is True
    assert contract.definition_hash != definition(old_value).definition_hash


def test_carry_permission_can_explicitly_allow_unbounded_age_and_requires_strict_marker():
    unlimited = FeatureField("price", "published value", "CNY", "per_symbol", "days", "mask",
                             allow_carry_forward=True)
    frame = compute_features(
        definition(lambda view, params: ({"A": {"price": make_cell(1, ((T1, "quarter"),), is_carried=True)}}, {}),
                   fields=(unlimited,)), View(cutoff=T3), {}, None,
    )
    assert frame.per_symbol["A"]["price"].value == 1
    assert frame.per_symbol["A"]["price"].age == 2
    with pytest.raises(ValueError, match="bool"):
        FeatureCell(1, 1, T1, 0, None, ("bar",))


def test_sourced_masked_cell_receives_age_without_becoming_valid():
    masked = FeatureCell(None, False, T1, None, "unavailable", ("announcement",))
    frame = compute_features(
        definition(lambda view, params: ({"A": {"price": masked}}, {})), View(), {}, None,
    )
    cell = frame.per_symbol["A"]["price"]
    assert cell.value is None
    assert cell.is_valid is False
    assert cell.age == 1


def test_direct_cell_and_frame_inputs_cannot_be_mutated_after_creation():
    refs = ["bar"]
    cell = FeatureCell(1, True, T1, 1, None, refs)
    refs.append("future")
    assert cell.source_refs == ("bar",)
    symbols = ["A", "B"]
    base = compute_features(definition(lambda view, params: ({}, {})), View(), {}, None)
    frame = replace(base, ordered_symbols=symbols)
    symbols.reverse()
    assert frame.ordered_symbols == ("A", "B")


def test_direct_frame_rejects_shape_and_future_source():
    base = compute_features(definition(lambda view, params: ({}, {})), View(), {}, None)
    with pytest.raises(ValueError, match="symbol|shape"):
        replace(base, per_symbol={"A": base.per_symbol["A"]})
    with pytest.raises(ValueError, match="field|shape"):
        replace(base, per_symbol={"A": {}, "B": base.per_symbol["B"]})
    future = FeatureCell(1, True, T3, 0, None, ("future",))
    with pytest.raises(ValueError, match="source|cutoff"):
        replace(base, per_symbol={"A": {"price": future}, "B": base.per_symbol["B"]})


def test_direct_frame_enforces_field_missing_policy_and_age_unit():
    base = compute_features(definition(lambda view, params: ({}, {})), View(), {}, None)
    rejecting = FeatureField("price", "close", "CNY", "per_symbol", "days", "reject")
    with pytest.raises(ValueError, match="missing|reject"):
        replace(base, field_schema=(rejecting,))
    wrong_age = FeatureCell(1, True, T1, 0, None, ("bar",))
    with pytest.raises(ValueError, match="age"):
        replace(base, per_symbol={"A": {"price": wrong_age}, "B": base.per_symbol["B"]})


def test_direct_frame_asof_must_match_cutoff_shanghai_date():
    base = compute_features(definition(lambda view, params: ({}, {})), View(cutoff=T1), {}, None)
    with pytest.raises(ValueError, match="asof|cutoff"):
        replace(base, asof=date(2024, 1, 3))
    rollover = datetime(2024, 1, 2, 17, tzinfo=timezone.utc)
    assert replace(base, signal_cutoff=rollover, asof=date(2024, 1, 3)).asof == date(2024, 1, 3)
    with pytest.raises(ValueError, match="asof|cutoff"):
        replace(base, signal_cutoff=rollover)


def test_direct_transform_training_end_must_match_last_cutoff_shanghai_date():
    contract = definition(lambda view, params: ({}, {}), fit=lambda frames, params: {},
                          apply=lambda frame, artifact, params: frame)
    raw = compute_fit_frame(contract, View(cutoff=T1), {})
    artifact = fit_transform(contract, (raw,), {})
    with pytest.raises(ValueError, match="training_end|cutoff"):
        replace(artifact, training_end=date(2024, 1, 3))
    rollover = datetime(2024, 1, 2, 17, tzinfo=timezone.utc)
    assert replace(artifact, training_cutoff=rollover, training_end=date(2024, 1, 3)).training_end == date(2024, 1, 3)
