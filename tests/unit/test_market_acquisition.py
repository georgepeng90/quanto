from dataclasses import replace
import time

import pytest

from quanto.data.contracts import BAR_COLUMNS, DataRequirement, MarketDataError, NormalizedChunk, PriceBasis
from tests.fixtures.market_data_cases import import_case, market_case


class FixtureProvider:
    name = "fixture"

    def __init__(self, frequency="1d", partial=False):
        self.requirement, self.bars, self.calendar = market_case(frequency)
        self.calls, self.partial = [], partial
        self.value = 10.

    def capabilities(self):
        from quanto.data.providers.base import ProviderCapabilities
        return ProviderCapabilities(("bars", "calendar"), (self.bars.request.frequency,), {"etf": BAR_COLUMNS[4:]}, ("raw", "vendor_research"))

    def fetch(self, request, context):
        context.remaining()
        self.calls.append(request)
        chunk = self.bars if request.kind == "bars" else self.calendar
        frame = chunk.frame.copy()
        if request.kind == "bars":
            frame = frame[frame.instrument_id.isin([item.key for item in request.instruments]) & (frame.bar_start >= request.start) & (frame.bar_end <= request.end)]
            if self.partial:
                frame = frame.iloc[:1]
            frame[["open", "high", "low", "close"]] = [self.value, self.value+1, self.value-1, self.value]
        if request.price_basis and request.price_basis.mode == "vendor_research":
            request = request.model_copy(update={"price_basis": PriceBasis(mode="vendor_research", basis_id="fixture_basis")})
        return NormalizedChunk(request, frame, chunk.source, chunk.evidence)


def coordinator(root, providers=()):
    from quanto.data.acquisition import AcquisitionCoordinator, MarketConfig
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore
    store, catalog = MarketStore(root), MarketCatalog(root)
    return AcquisitionCoordinator(store, catalog, providers, MarketConfig(max_retries=0, retry_delay_seconds=0)), catalog


def prepare(engine, catalog, requirement, identity="request", refresh=False):
    catalog.create_request(identity, requirement)
    return engine.prepare(requirement, refresh, lambda: (identity,), lambda report: None, lambda: False)


def test_repeat_requirement_reuses_snapshot_without_source_calls(tmp_path):
    provider = FixtureProvider()
    engine, catalog = coordinator(tmp_path, (provider,))
    first = prepare(engine, catalog, provider.requirement)
    count = len(provider.calls)
    second = prepare(engine, catalog, provider.requirement, "second")
    assert first == second
    assert len(provider.calls) == count
    assert catalog.resolve_snapshot(first.id) == first


def test_complete_offline_minute_prefetch_bypasses_capabilities(tmp_path):
    _, _, requirement = import_case(tmp_path)
    engine, catalog = coordinator(tmp_path)
    snapshot = prepare(engine, catalog, requirement)
    assert snapshot.coverage.complete


def test_only_actual_gaps_are_fetched(tmp_path):
    import pandas as pd
    from quanto.data.imports import import_table

    store, catalog, requirement = import_case(tmp_path, "1d")
    provider = FixtureProvider()
    # Extend the demand by one actual session; keep the old immutable rows cached.
    bars = provider.bars.frame[provider.bars.frame.trading_date == pd.Timestamp("2024-02-01").date()].copy()
    bars["bar_start"] += pd.Timedelta(days=1)
    bars["bar_end"] += pd.Timedelta(days=1)
    bars["trading_date"] = pd.Timestamp("2024-02-02").date()
    provider.bars = replace(provider.bars, frame=bars)
    end = pd.Timestamp("2024-02-03T00:00Z").to_pydatetime()
    request = requirement.requests[0].model_copy(update={"end": end})
    calendar_request = requirement.requests[1].model_copy(update={"end": end})
    calendar_frame = provider.calendar.frame.copy()
    last = calendar_frame.iloc[[-1]].copy()
    last["trading_date"] = pd.Timestamp("2024-02-02").date()
    last["session_start"] += pd.Timedelta(days=1)
    last["session_end"] += pd.Timedelta(days=1)
    import_table(store, catalog, calendar_request, pd.concat([calendar_frame, last]), provider.calendar.source, b"extended calendar")
    requirement = DataRequirement(requests=(request, calendar_request))
    engine, catalog = coordinator(tmp_path, (provider,))
    assert prepare(engine, catalog, requirement).coverage.complete
    assert len(provider.calls) == 2
    assert all(call.start == pd.Timestamp("2024-02-02T01:30Z") for call in provider.calls)


def test_partial_or_missing_required_evidence_is_incomplete(tmp_path):
    provider = FixtureProvider(partial=True)
    engine, catalog = coordinator(tmp_path, (provider,))
    with pytest.raises(MarketDataError) as failure:
        prepare(engine, catalog, provider.requirement)
    assert failure.value.code == "INCOMPLETE"
    assert failure.value.coverage and not failure.value.coverage.complete


def test_refresh_preserves_old_snapshot_and_publishes_new_bytes(tmp_path):
    provider = FixtureProvider()
    engine, catalog = coordinator(tmp_path, (provider,))
    first = prepare(engine, catalog, provider.requirement)
    previous = engine.store.verify_snapshot(first)
    provider.value = 20.
    second = prepare(engine, catalog, provider.requirement, "refresh", True)
    assert first.id != second.id
    assert engine.store.verify_snapshot(first) == previous
    assert second.coverage.complete


def test_daily_fragments_have_independent_instrument_identity(tmp_path):
    from quanto.data.storage import MarketStore
    _, bars, _ = market_case("1d")
    fragments = MarketStore(tmp_path).put_chunk(bars)
    assert len(fragments) == 2
    assert all(len(fragment.request.instruments) == 1 for fragment in fragments)


def test_incompatible_unknown_basis_cannot_be_spliced(tmp_path):
    from quanto.data.contracts import PriceBasis
    from quanto.data.imports import import_table
    from quanto.data.storage import MarketStore
    from quanto.data.catalog import MarketCatalog

    requirement, bars, calendar = market_case("1d")
    wildcard = bars.request.model_copy(update={"price_basis": PriceBasis(mode="vendor_research")})
    requirement = DataRequirement(requests=(wildcard, calendar.request))
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    import_table(store, catalog, calendar.request, calendar.frame, calendar.source, calendar.evidence)
    for basis in ("first", "second"):
        resolved = wildcard.model_copy(update={"price_basis": PriceBasis(mode="vendor_research", basis_id=basis)})
        import_table(store, catalog, resolved, bars.frame, bars.source, basis.encode())
    engine, catalog = coordinator(tmp_path)
    with pytest.raises(MarketDataError) as failure:
        prepare(engine, catalog, requirement)
    assert failure.value.code == "INCOMPLETE"
    assert any(entry.reason == "SEMANTIC_CONFLICT" for entry in failure.value.coverage.entries)


def test_incomplete_refresh_preserves_previous_active_cache(tmp_path):
    provider = FixtureProvider()
    engine, catalog = coordinator(tmp_path, (provider,))
    original = prepare(engine, catalog, provider.requirement)
    provider.partial = True
    with pytest.raises(MarketDataError) as failure:
        prepare(engine, catalog, provider.requirement, "partial-refresh", True)
    assert failure.value.code == "INCOMPLETE"
    assert prepare(engine, catalog, provider.requirement, "reuse") == original


def test_adjustment_revision_refreshes_affected_known_history(tmp_path):
    import pandas as pd
    provider = FixtureProvider()
    requirement = provider.requirement.model_copy(update={"requests": (
        provider.requirement.requests[0].model_copy(update={"price_basis": PriceBasis(mode="vendor_research")}), provider.requirement.requests[1])})
    engine, catalog = coordinator(tmp_path, (provider,))
    original = prepare(engine, catalog, requirement)
    bars = provider.bars.frame.copy()
    last = bars[bars.trading_date == pd.Timestamp("2024-02-01").date()].copy()
    last["trading_date"] = pd.Timestamp("2024-02-02").date()
    last["bar_start"] += pd.Timedelta(days=1)
    last["bar_end"] += pd.Timedelta(days=1)
    provider.bars = replace(provider.bars, frame=pd.concat([bars, last]))
    sessions = provider.calendar.frame.copy()
    last_session = sessions.iloc[[-1]].copy()
    last_session["trading_date"] = pd.Timestamp("2024-02-02").date()
    last_session["session_start"] += pd.Timedelta(days=1)
    last_session["session_end"] += pd.Timedelta(days=1)
    provider.calendar = replace(provider.calendar, frame=pd.concat([sessions, last_session]))
    end = pd.Timestamp("2024-02-03T00:00Z").to_pydatetime()
    extended = requirement.model_copy(update={"requests": tuple(item.model_copy(update={"end": end}) for item in requirement.requests)})
    provider.calls.clear()
    provider.value = 20.
    updated = prepare(engine, catalog, extended, "extension")
    assert updated.id != original.id
    assert all(call.start == requirement.requests[0].start for call in provider.calls if call.kind == "bars")
    assert engine.store.verify_snapshot(original).coverage.complete


def test_disjoint_gaps_do_not_refetch_known_sessions(tmp_path):
    import pandas as pd
    from quanto.data.imports import import_table
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore
    provider = FixtureProvider()
    bars, calendar = provider.bars.frame, provider.calendar.frame
    expanded_bars, expanded_calendar = [], []
    for shift in (0, 2):
        b, c = bars.copy(), calendar.copy()
        for column in ("bar_start", "bar_end"):
            b[column] += pd.Timedelta(days=shift)
        for column in ("session_start", "session_end"):
            c[column] += pd.Timedelta(days=shift)
        b["trading_date"] = b.bar_start.dt.date
        c["trading_date"] = c.session_start.dt.date
        expanded_bars.append(b)
        expanded_calendar.append(c)
    frame, calendar_frame = pd.concat(expanded_bars), pd.concat(expanded_calendar)
    end = pd.Timestamp("2024-02-04T00:00Z").to_pydatetime()
    requirement = provider.requirement.model_copy(update={"requests": tuple(item.model_copy(update={"end": end}) for item in provider.requirement.requests)})
    provider.bars = replace(provider.bars, frame=frame)
    provider.calendar = replace(provider.calendar, frame=calendar_frame)
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    known = frame[frame.trading_date.isin([pd.Timestamp("2024-01-31").date(), pd.Timestamp("2024-02-02").date()])]
    import_table(store, catalog, requirement.requests[0], known, provider.bars.source, b"known disjoint sessions")
    import_table(store, catalog, requirement.requests[1], calendar_frame, provider.calendar.source, b"four fixture sessions")
    engine, catalog = coordinator(tmp_path, (provider,))
    assert prepare(engine, catalog, requirement).coverage.complete
    assert len(provider.calls) == 4
    assert all(call.start.date() == call.end.date() for call in provider.calls)


def test_new_instrument_does_not_refetch_existing_instrument(tmp_path):
    from quanto.data.imports import import_table
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore
    provider = FixtureProvider()
    first, second = provider.bars.request.instruments
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    first_request = provider.bars.request.model_copy(update={"instruments": (first,)})
    import_table(store, catalog, first_request, provider.bars.frame[provider.bars.frame.instrument_id == first.key], provider.bars.source, provider.bars.evidence)
    import_table(store, catalog, provider.calendar.request, provider.calendar.frame, provider.calendar.source, provider.calendar.evidence)
    engine, catalog = coordinator(tmp_path, (provider,))
    assert prepare(engine, catalog, provider.requirement).coverage.complete
    assert len(provider.calls) == 1
    assert provider.calls[0].instruments == (second,)


def test_unknown_required_fact_cannot_become_ready(tmp_path):
    from quanto.data.contracts import QualityPolicy
    provider = FixtureProvider()
    requirement = provider.requirement.model_copy(update={"quality": QualityPolicy(required_evidence=("availability",))})
    engine, catalog = coordinator(tmp_path, (provider,))
    with pytest.raises(MarketDataError) as failure:
        prepare(engine, catalog, requirement)
    assert failure.value.code == "INCOMPLETE"
    assert "availability" in failure.value.coverage.entries[0].missing_evidence


@pytest.mark.parametrize("deficiency", ["partial", "quality"])
def test_incomplete_preferred_source_tries_compatible_alternative(tmp_path, deficiency):
    from quanto.data.contracts import QualityPolicy
    first, second = FixtureProvider(partial=deficiency == "partial"), FixtureProvider()
    first.name, second.name = "preferred", "alternate"
    requirement = first.requirement
    if deficiency == "quality":
        first.bars = replace(first.bars, source=first.bars.source.model_copy(update={"verified": ("price_basis",)}))
        requirement = requirement.model_copy(update={"quality": QualityPolicy(required_evidence=("units",))})
    engine, catalog = coordinator(tmp_path, (first, second))
    assert prepare(engine, catalog, requirement).coverage.complete
    assert len(second.calls) == 2
    count = len(first.calls) + len(second.calls)
    assert prepare(engine, catalog, requirement, "reuse").coverage.complete
    assert len(first.calls) + len(second.calls) == count


def test_gap_fallback_checks_units_against_cached_history(tmp_path):
    from quanto.data.imports import import_table
    from quanto.data.storage import MarketStore
    from quanto.data.catalog import MarketCatalog
    first, second = FixtureProvider(), FixtureProvider()
    first.name, second.name = "preferred", "alternate"
    first.bars = replace(first.bars, source=first.bars.source.model_copy(update={"units": {"volume": "unknown", "amount": "unknown"}, "verified": ("price_basis",)}))
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    prefix = second.bars.frame.loc[second.bars.frame.trading_date == second.calendar.frame.trading_date.min()]
    import_table(store, catalog, second.bars.request, prefix, second.bars.source, b"prefix")
    import_table(store, catalog, second.calendar.request, second.calendar.frame, second.calendar.source, second.calendar.evidence)
    engine, catalog = coordinator(tmp_path, (first, second))
    assert prepare(engine, catalog, second.requirement).coverage.complete
    assert len(first.calls) == len(second.calls) == 2
