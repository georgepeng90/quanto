from dataclasses import replace

import pandas as pd
import pytest

from quanto.data import MarketData
from quanto.data.contracts import DataRequest, DataRequirement, MarketDataError, PriceBasis
from quanto.data.imports import import_table
from quanto.data.validation import validate_chunk
from tests.fixtures.market_data_cases import market_case


def test_conflicting_units_within_instrument_block_ready(tmp_path):
    requirement, bars, calendar = market_case("1d")
    with MarketData(tmp_path, providers=()) as market:
        import_table(market.store, market.catalog, calendar.request, calendar.frame, calendar.source, calendar.evidence)
        for day, unit in (("2024-01-31", "share"), ("2024-02-01", "lot100")):
            frame = bars.frame.loc[bars.frame.trading_date == pd.Timestamp(day).date()]
            source = bars.source.model_copy(update={"units": {"volume": unit, "amount": "CNY"}, "verified": ("price_basis",)})
            import_table(market.store, market.catalog, bars.request, frame, source, day.encode())
        with pytest.raises(MarketDataError) as failure:
            market.wait_ready(market.prefetch(requirement).id, 10)
        assert failure.value.coverage.entries[0].reason == "SEMANTIC_CONFLICT"


@pytest.mark.parametrize("units", [{"volume": "lot100", "amount": "CNY"}, {"volume": "share", "amount": "USD"}])
def test_verified_units_must_be_normalized_to_declared_currency(units):
    _, bars, _ = market_case("1d")
    with pytest.raises(MarketDataError, match="verified units"):
        validate_chunk(replace(bars, source=bars.source.model_copy(update={"units": units})))


def test_import_requires_resolved_research_basis(tmp_path):
    _, bars, _ = market_case("1d")
    request = bars.request.model_copy(update={"price_basis": PriceBasis(mode="vendor_research")})
    with MarketData(tmp_path, providers=()) as market:
        with pytest.raises(MarketDataError, match="resolved research basis"):
            import_table(market.store, market.catalog, request, bars.frame, bars.source, bars.evidence)
        assert not market.catalog.fragments(DataRequirement(requests=(request,)))


def test_unresolved_persisted_research_basis_cannot_become_ready(tmp_path):
    _, bars, calendar = market_case("1d")
    request = bars.request.model_copy(update={"price_basis": PriceBasis(mode="vendor_research")})
    with MarketData(tmp_path, providers=()) as market:
        import_table(market.store, market.catalog, calendar.request, calendar.frame, calendar.source, calendar.evidence)
        market.catalog.register_fragments(market.store.put_chunk(replace(bars, request=request)))
        with pytest.raises(MarketDataError) as failure:
            market.wait_ready(market.prefetch(DataRequirement(requests=(request, calendar.request))).id, 10)
        assert failure.value.coverage.entries[0].reason == "UNRESOLVED_PRICE_BASIS"


@pytest.mark.parametrize("kind,columns,proof", [
    ("corporate_actions", ("instrument_id", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"), "actions"),
    ("trading_status", ("instrument_id", "trading_date", "status"), "status"),
])
def test_reference_evidence_must_cover_each_requested_instrument(tmp_path, kind, columns, proof):
    _, bars, _ = market_case("1d")
    request = DataRequest(kind=kind, instruments=bars.request.instruments, start=bars.request.start, end=bars.request.end, columns=columns)
    source = bars.source.model_copy(update={"verified": (proof,)})
    with MarketData(tmp_path, providers=()) as market:
        for index, instrument in enumerate(request.instruments):
            part = request.model_copy(update={"instruments": (instrument,)})
            import_table(market.store, market.catalog, part, pd.DataFrame(columns=columns), source, str(index).encode())
            task = market.prefetch(DataRequirement(requests=(request,)))
            if index == 0:
                with pytest.raises(MarketDataError) as failure:
                    market.wait_ready(task.id, 10)
                assert proof in failure.value.coverage.entries[0].missing_evidence
            else:
                assert market.wait_ready(task.id, 10).coverage.complete


def test_reference_fields_must_cover_each_requested_instrument(tmp_path):
    _, bars, _ = market_case("1d")
    request = DataRequest(kind="trading_status", instruments=bars.request.instruments, start=bars.request.start, end=bars.request.end,
        columns=("instrument_id", "trading_date", "status", "can_buy"))
    source = bars.source.model_copy(update={"verified": ("status",)})
    with MarketData(tmp_path, providers=()) as market:
        for index, instrument in enumerate(request.instruments):
            columns = request.columns if index else request.columns[:-1]
            part = request.model_copy(update={"instruments": (instrument,), "columns": columns})
            frame = pd.DataFrame([{"instrument_id": instrument.key, "trading_date": bars.frame.trading_date.min(), "status": "trading", "can_buy": True}])[list(columns)]
            import_table(market.store, market.catalog, part, frame, source, str(index).encode())
        with pytest.raises(MarketDataError) as failure:
            market.wait_ready(market.prefetch(DataRequirement(requests=(request,))).id, 10)
        assert "can_buy" in failure.value.coverage.entries[0].missing_fields


def test_null_trading_date_is_not_normalized_data():
    _, bars, _ = market_case("1d")
    frame = bars.frame.copy()
    frame.loc[0, "trading_date"] = None
    with pytest.raises(MarketDataError):
        validate_chunk(replace(bars, frame=frame))


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_trading_dates_follow_calendar_session_labels(tmp_path, frequency):
    requirement, bars, calendar = market_case(frequency)
    # Overnight markets may label a UTC session with the following trading date.
    dates = {day: day + pd.Timedelta(days=1) for day in calendar.frame.trading_date}
    calendar = replace(calendar, frame=calendar.frame.assign(trading_date=lambda frame: frame.trading_date.map(dates)))
    with MarketData(tmp_path, providers=()) as market:
        import_table(market.store, market.catalog, calendar.request, calendar.frame, calendar.source, calendar.evidence)
        bad = import_table(market.store, market.catalog, bars.request, bars.frame, bars.source, bars.evidence)
        with pytest.raises(MarketDataError) as failure:
            market.wait_ready(market.prefetch(requirement).id, 10)
        assert failure.value.coverage.entries[0].reason == "TRADING_DATE_MISMATCH"
        corrected = bars.frame.assign(trading_date=lambda frame: frame.trading_date.map(dates))
        refs = market.store.put_chunk(validate_chunk(replace(bars, frame=corrected, evidence=b"correct session labels")))
        market.catalog.create_request("repair", requirement)
        market.catalog.publish_fragments(bad, refs, (), ("repair",))
        assert market.wait_ready(market.prefetch(requirement).id, 10).coverage.complete


@pytest.mark.parametrize("kind,proof", [("trading_status", "status"), ("instruments", "instruments")])
def test_conflicting_reference_facts_block_ready(tmp_path, kind, proof):
    requirement, bars, calendar = market_case("1d")
    missing = bars.frame.iloc[0]
    instrument = next(item for item in bars.request.instruments if item.key == missing.instrument_id)
    if kind == "trading_status":
        rows = [dict(instrument_id=instrument.key, trading_date=missing.trading_date, status=value) for value in ("suspended", "trading")]
    else:
        rows = [dict(instrument_id=instrument.key, listing_date=pd.Timestamp(value).date()) for value in ("2024-01-31", "2024-02-02")]
    request = DataRequest(kind=kind, instruments=(instrument,), start=bars.request.start, end=bars.request.end, columns=tuple(rows[0]))
    source = bars.source.model_copy(update={"verified": (proof,)})
    with MarketData(tmp_path, providers=()) as market:
        for chunk in (replace(bars, frame=bars.frame.iloc[1:]), calendar):
            import_table(market.store, market.catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
        for index, row in enumerate(rows):
            import_table(market.store, market.catalog, request, pd.DataFrame([row]), source, str(index).encode())
        wanted = DataRequirement(requests=(*requirement.requests, request)) if kind == "trading_status" else DataRequirement(requests=(request,))
        with pytest.raises(MarketDataError) as failure:
            market.wait_ready(market.prefetch(wanted).id, 10)
        assert any(entry.reason == "SEMANTIC_CONFLICT" for entry in failure.value.coverage.entries)
        from quanto.data.contracts import NormalizedChunk
        from quanto.data.providers.base import ProviderCapabilities
        from tests.unit.test_market_acquisition import coordinator, prepare
        class ReferenceProvider:
            name = "revision"
            def capabilities(self):
                return ProviderCapabilities((kind,), (), {}, ())
            def fetch(self, demand, context):
                return NormalizedChunk(demand, pd.DataFrame([rows[0]]), source, b"explicit revision")
        engine, catalog = coordinator(tmp_path, (ReferenceProvider(),))
        repaired = prepare(engine, catalog, DataRequirement(requests=(request,)), "reference-refresh", refresh=True)
        assert repaired.coverage.complete
