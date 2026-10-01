from dataclasses import replace

import pytest

from quanto.data.contracts import DataRequest, DataRequirement, MarketDataError, NormalizedChunk
from tests.fixtures.market_data_cases import import_case, market_case


def test_imported_minute_coverage_needs_no_live_provider(tmp_path):
    from quanto.data.coverage import evaluate_coverage

    store, catalog, requirement = import_case(tmp_path)
    assert evaluate_coverage(requirement, catalog.fragments(requirement), store).complete
    assert len(list(tmp_path.glob("objects/*/*.parquet"))) == 3


@pytest.mark.parametrize("frequency", ["1d", "1m"])
def test_unknown_middle_gap_remains_incomplete(tmp_path, frequency):
    from quanto.data.coverage import evaluate_coverage
    from quanto.data.imports import import_table
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore

    requirement, bars, calendar = market_case(frequency)
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    for chunk in (replace(bars, frame=bars.frame.drop(bars.frame.index[1])), calendar):
        import_table(store, catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
    coverage = evaluate_coverage(requirement, catalog.fragments(requirement), store)
    assert not coverage.complete
    assert coverage.entries[0].missing


def test_evidenced_suspension_and_listing_boundaries_pass(tmp_path):
    import pandas as pd
    from quanto.data.coverage import evaluate_coverage
    from quanto.data.imports import import_table
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore

    requirement, bars, calendar = market_case("1d")
    removed = bars.frame.iloc[0]
    bars = replace(bars, frame=bars.frame.iloc[1:])
    status_request = DataRequest(kind="trading_status", instruments=bars.request.instruments, start=bars.request.start,
                                end=bars.request.end, columns=("instrument_id", "trading_date", "status"))
    status = NormalizedChunk(status_request, pd.DataFrame([dict(instrument_id=removed.instrument_id, trading_date=removed.trading_date, status="suspended")]),
                             bars.source.model_copy(update={"verified": ("status",)}), b"synthetic suspension")
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    for chunk in (bars, calendar, status):
        import_table(store, catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
    assert evaluate_coverage(requirement, catalog.fragments(requirement) + catalog.fragments(DataRequirement(requests=(status_request,))), store).complete


def test_listing_boundary_only_exempts_missing_bars_with_evidence(tmp_path):
    import pandas as pd
    from quanto.data.coverage import evaluate_coverage
    from quanto.data.imports import import_table
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore

    requirement, bars, calendar = market_case("1d")
    instrument = bars.request.instruments[1]
    listing = pd.Timestamp("2024-02-01").date()
    bars = replace(bars, frame=bars.frame[~((bars.frame.instrument_id == instrument.key) & (bars.frame.trading_date < listing))])
    request = DataRequest(kind="instruments", instruments=(instrument,), start=bars.request.start, end=bars.request.end, columns=("instrument_id", "listing_date"))
    data = pd.DataFrame([dict(instrument_id=instrument.key, listing_date=listing)])
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    for chunk in (bars, calendar):
        import_table(store, catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
    unknown = import_table(store, catalog, request, data, bars.source, b"unverified listing")
    refs = catalog.fragments(requirement)
    assert not evaluate_coverage(requirement, refs + unknown, store).complete
    proven = import_table(store, catalog, request, data, bars.source.model_copy(update={"verified": ("instruments",)}), b"verified listing fixture")
    assert evaluate_coverage(requirement, refs + proven, store).complete


def test_bar_outside_verified_session_blocks_ready(tmp_path):
    import pandas as pd
    from quanto.data.coverage import evaluate_coverage
    from quanto.data.imports import import_table
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore

    requirement, bars, calendar = market_case()
    extra = bars.frame.iloc[[0]].copy()
    extra["bar_start"] += pd.Timedelta(hours=1)
    extra["bar_end"] += pd.Timedelta(hours=1)
    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    for chunk in (replace(bars, frame=pd.concat([bars.frame, extra])), calendar):
        import_table(store, catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
    report = evaluate_coverage(requirement, catalog.fragments(requirement), store)
    assert not report.complete
    assert report.entries[0].reason == "BAR_OUTSIDE_SESSION"


@pytest.mark.parametrize("damage", ["naive", "ohlc", "duplicate", "unknown_instrument"])
def test_invalid_standard_bars_are_rejected(damage):
    from quanto.data.validation import validate_chunk

    _, bars, _ = market_case()
    frame = bars.frame.copy()
    if damage == "naive":
        frame["bar_start"] = frame.bar_start.dt.tz_localize(None)
    elif damage == "ohlc":
        frame.loc[0, "high"] = 1
    elif damage == "duplicate":
        import pandas as pd
        changed = frame.iloc[[0]].copy()
        changed["close"] = 10.5
        frame = pd.concat([frame, changed])
    else:
        frame.loc[0, "instrument_id"] = "SZSE:etf:unknown"
    with pytest.raises(MarketDataError):
        validate_chunk(replace(bars, frame=frame))
