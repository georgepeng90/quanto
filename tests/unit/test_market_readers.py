import pyarrow as pa
import pytest

from quanto.data.contracts import DataQuery, MarketDataError, SnapshotManifest, TableQuery
from tests.fixtures.market_data_cases import import_case


def prepared(root):
    from quanto.data.coverage import evaluate_coverage
    from quanto.data.readers import MarketReader

    store, catalog, requirement = import_case(root)
    fragments = catalog.fragments(requirement)
    snapshot = store.write_manifest(SnapshotManifest(requirement=requirement, fragments=fragments,
        coverage=evaluate_coverage(requirement, fragments, store)))
    return MarketReader(store), snapshot, requirement


def test_batches_are_bounded_and_globally_ordered(tmp_path):
    reader, snapshot, requirement = prepared(tmp_path)
    query = DataQuery(**requirement.requests[0].model_dump())
    batches = list(reader.iter_batches(snapshot, query, batch_size=2))
    assert all(batch.num_rows <= 2 for batch in batches)
    frame = pa.Table.from_batches(batches).to_pandas()
    keys = list(frame[["bar_end", "instrument_id", "bar_start"]].itertuples(index=False, name=None))
    assert keys == sorted(keys)
    assert len(frame) == 8
    assert frame.instrument_id.iloc[0] == "SZSE:etf:000001"


def test_mixed_timestamp_units_preserve_nanoseconds_across_months(tmp_path):
    import pandas as pd
    from quanto.data import MarketData
    from quanto.data.imports import import_table
    from tests.fixtures.market_data_cases import market_case
    requirement, bars, calendar = market_case()
    for column in ("bar_start", "bar_end"):
        values = bars.frame[column].astype("datetime64[ns, UTC]")
        bars.frame[column] = values + pd.to_timedelta((values.dt.month == 2).astype(int) * 123, unit="ns")
    for column in ("session_start", "session_end"):
        values = calendar.frame[column].astype("datetime64[ns, UTC]")
        calendar.frame[column] = values + pd.to_timedelta((values.dt.month == 2).astype(int) * 123, unit="ns")
    with MarketData(tmp_path, providers=()) as market:
        # Preserve a real mixed-unit file set: January microseconds, February nanoseconds.
        for month, unit in ((1, "us"), (2, "ns")):
            frame = bars.frame.loc[bars.frame.bar_start.dt.month == month].copy()
            for column in ("bar_start", "bar_end"):
                frame[column] = frame[column].astype(f"datetime64[{unit}, UTC]")
            import_table(market.store, market.catalog, bars.request, frame, bars.source, b"mixed timestamp units")
        import_table(market.store, market.catalog, calendar.request, calendar.frame, calendar.source, calendar.evidence)
        snapshot = market.wait_ready(market.prefetch(requirement).id, 10)
        result = market.read_bars(snapshot, DataQuery(**bars.request.model_dump()))
        february = result.loc[result.bar_start.dt.month == 2]
        assert len(february) == 4
        assert set(february.bar_start.dt.nanosecond) == {123}
        assert set(february.bar_end.dt.nanosecond) == {123}


def test_reference_and_auxiliary_queries_stay_within_prepared_bounds(tmp_path):
    from datetime import timedelta

    reader, snapshot, requirement = prepared(tmp_path)
    calendar = requirement.requests[1]
    query = TableQuery(start=calendar.start, end=calendar.end, columns=calendar.columns)
    assert len(reader.read_table(snapshot, "calendar", query)) == 2
    for table, bad in [("calendar", query.model_copy(update={"end": query.end + timedelta(days=1)})), ("auxiliary:unknown", query),
                       ("calendar", query.model_copy(update={"columns": ("bar_start",)}))]:
        with pytest.raises(MarketDataError) as failure:
            reader.read_table(snapshot, table, bad)
        assert failure.value.code == "DATA_NOT_PREPARED"


def test_unprepared_bar_field_is_rejected(tmp_path):
    reader, snapshot, requirement = prepared(tmp_path)
    query = DataQuery(**(requirement.requests[0].model_dump() | {"columns": ("unprepared",)}))
    with pytest.raises(MarketDataError) as failure:
        reader.read_bars(snapshot, query)
    assert failure.value.code == "DATA_NOT_PREPARED"


def test_auxiliary_available_at_survives_import_and_local_query(tmp_path):
    import pandas as pd
    from quanto.data.contracts import DataRequest, DataRequirement
    from quanto.data.coverage import evaluate_coverage
    from quanto.data.imports import import_table
    from quanto.data.readers import MarketReader

    store, catalog, original = import_case(tmp_path)
    bars = original.requests[0]
    request = DataRequest(kind="auxiliary:news", start=bars.start, end=bars.end, columns=("available_at", "value"))
    source = catalog.fragments(original)[0].source.model_copy(update={"verified": ("auxiliary:news",)})
    frame = pd.DataFrame({"available_at": pd.to_datetime(["2024-01-31T02:00Z", "2024-02-01T02:00Z"]), "value": [1, 2]})
    import_table(store, catalog, request, frame, source, b"fixture publications")
    requirement = DataRequirement(requests=original.requests + (request,))
    fragments = catalog.fragments(requirement)
    snapshot = store.write_manifest(SnapshotManifest(requirement=requirement, fragments=fragments, coverage=evaluate_coverage(requirement, fragments, store)))
    query = TableQuery(start=bars.start, end=pd.Timestamp("2024-02-01T00:00Z"), columns=request.columns)
    result = MarketReader(store).read_table(snapshot, request.kind, query)
    assert result.value.tolist() == [1]
    assert str(result.available_at.dtype).endswith(", UTC]")
