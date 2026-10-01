from __future__ import annotations

from itertools import groupby
import heapq

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quanto.data.catalog import compatible
from quanto.data.contracts import BAR_KEY, BAR_ORDER, DataQuery, DataRequest, MarketDataError, SnapshotRef, TableQuery
from quanto.data.coverage import fragment_frame
from quanto.data.storage import MarketStore
from quanto.data.validation import REFERENCE_KEYS, merge_reference


class MarketReader:
    def __init__(self, store: MarketStore):
        self.store = store

    def _prepared(self, snapshot, request):
        manifest = self.store.verify_snapshot(snapshot)
        for prepared in manifest.requirement.requests:
            implicit = set(BAR_KEY).union({"trading_date"}) if request.kind == "bars" else set()
            scope = request
            if prepared.price_basis and prepared.price_basis.mode == "vendor_research" and not prepared.price_basis.basis_id:
                if not any(compatible(ref.request, request) for ref in manifest.fragments):
                    continue
                scope = request.model_copy(update={"price_basis": prepared.price_basis})
            if compatible(prepared, scope) and prepared.start <= request.start and prepared.end >= request.end and set(request.columns).issubset(set(prepared.columns).union(implicit)):
                if set(request.instruments).issubset(prepared.instruments):
                    return manifest
        raise MarketDataError("DATA_NOT_PREPARED", "query exceeds snapshot requirement")

    def iter_batches(self, snapshot: SnapshotRef, query: DataQuery, batch_size: int):
        if batch_size <= 0:
            raise ValueError("positive batch_size required")
        manifest = self._prepared(snapshot, query)
        refs = [ref for ref in manifest.fragments if compatible(ref.request, query)]
        if not refs:
            return
        columns = list(dict.fromkeys((*BAR_KEY, "trading_date", *query.columns)))
        refs.sort(key=lambda item: item.request.start)
        groups = [refs] if query.frequency == "1d" else [list(group) for _, group in groupby(refs, key=lambda item: item.request.start.strftime("%Y-%m"))]
        ids = {item.key for item in query.instruments}
        output, previous = [], None
        schema = pq.ParquetFile(self.store.object_path(refs[0])).schema_arrow
        timestamps = ("bar_start", "bar_end")
        schema = pa.schema([pa.field(name, pa.timestamp("ns", tz="UTC")) if name in timestamps else schema.field(name) for name in columns])
        start_ns, end_ns = pd.Timestamp(query.start).value, pd.Timestamp(query.end).value
        for group in groups:
            input_size = max(1, batch_size // max(1, len(group)))
            def rows(ref):
                with pq.ParquetFile(self.store.object_path(ref)) as parquet:
                    for batch in parquet.iter_batches(batch_size=input_size, columns=columns):
                        # Arrow's scalar timezone conversion imports optional pytz per value.
                        # Compare integer nanoseconds and construct timezone-aware output in Arrow.
                        for name in timestamps:
                            index = batch.schema.get_field_index(name)
                            values = batch.column(index).cast(pa.timestamp("ns", tz="UTC")).cast(pa.int64())
                            batch = batch.set_column(index, name, values)
                        for row in batch.to_pylist():
                            if row["instrument_id"] in ids and row["bar_start"] >= start_ns and row["bar_end"] <= end_ns:
                                yield row
            streams = [iter(rows(ref)) for ref in group]
            heap = []
            for index, stream in enumerate(streams):
                row = next(stream, None)
                if row is not None:
                    heapq.heappush(heap, (tuple(row[name] for name in BAR_ORDER), index, row))
            while heap:
                _, index, row = heapq.heappop(heap)
                if previous is None or tuple(row[name] for name in BAR_KEY) != tuple(previous[name] for name in BAR_KEY):
                    output.append(row)
                    previous = row
                elif row != previous:
                    raise MarketDataError("SEMANTIC_CONFLICT", "conflicting snapshot rows")
                if len(output) == batch_size:
                    yield pa.RecordBatch.from_pylist(output, schema=schema)
                    output = []
                following = next(streams[index], None)
                if following is not None:
                    heapq.heappush(heap, (tuple(following[name] for name in BAR_ORDER), index, following))
        if output:
            yield pa.RecordBatch.from_pylist(output, schema=schema)

    def read_bars(self, snapshot: SnapshotRef, query: DataQuery) -> pd.DataFrame:
        batches = list(self.iter_batches(snapshot, query, 65536))
        if not batches:
            return pd.DataFrame(columns=list(dict.fromkeys((*BAR_KEY, "trading_date", *query.columns))))
        return pa.Table.from_batches(batches).to_pandas()

    def read_table(self, snapshot: SnapshotRef, table: str, query: TableQuery) -> pd.DataFrame:
        request = DataRequest(kind=table, **query.model_dump())
        manifest = self._prepared(snapshot, request)
        refs = [ref for ref in manifest.fragments if compatible(ref.request, request)]
        frame = pd.concat([fragment_frame(self.store, ref) for ref in refs], ignore_index=True).drop_duplicates()
        if query.instruments and "instrument_id" in frame:
            frame = frame[frame.instrument_id.isin([item.key for item in query.instruments])]
        if table == "calendar":
            frame = frame[(frame.session_start >= query.start) & (frame.session_end <= query.end)]
        elif table == "trading_status":
            dates = pd.to_datetime(frame.trading_date, utc=True)
            frame = frame[(dates >= query.start) & (dates < query.end)]
        elif table == "corporate_actions":
            dates = [pd.to_datetime(frame[name], utc=True) for name in ("record_date", "ex_date", "pay_date")]
            keep = pd.Series(False, index=frame.index)
            for values in dates:
                keep |= (values >= query.start) & (values < query.end)
            frame = frame[keep]
        elif table.startswith("auxiliary:"):
            frame = frame[frame.available_at.isna() | ((frame.available_at >= query.start) & (frame.available_at < query.end))]
        if table in REFERENCE_KEYS:
            frame = merge_reference(frame, REFERENCE_KEYS[table])
        return frame[list(query.columns)].reset_index(drop=True)
