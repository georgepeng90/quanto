from datetime import datetime, timedelta, timezone

import pandas as pd

from quanto.data.catalog import MarketCatalog
from quanto.data.contracts import CoverageEntry, CoverageReport, DataQuery, DataRequest, DataRequirement, InstrumentId, NormalizedChunk, PriceBasis, SnapshotManifest, SourceRecord
from quanto.data.storage import MarketStore


def learning_case(root, minutes=90, missing=(), spike_after=None, instrument_count=1, missing_by_instrument=None):
    start = datetime(2024, 1, 2, 1, 30, tzinfo=timezone.utc)
    end = start + timedelta(hours=5)
    instruments = tuple(InstrumentId(exchange="SZSE", instrument_type="etf", code=f"{index + 1:06d}") for index in range(instrument_count))
    query = DataQuery(kind="bars", instruments=instruments, start=start, end=end,
        columns=("open", "high", "low", "close", "volume", "amount"), frequency="1m", price_basis=PriceBasis(mode="raw"))
    calendar_request = DataRequest(kind="calendar", start=start, end=end, columns=("trading_date", "session_start", "session_end"))
    sessions = [(start, start + timedelta(minutes=minutes)), (start + timedelta(hours=3), start + timedelta(hours=3, minutes=10))]
    rows = []
    for begin, finish in sessions:
        for index in range(int((finish - begin).total_seconds() / 60)):
            stamp = begin + timedelta(minutes=index)
            if stamp in missing:
                continue
            price = 10 + index * .01 + (.01 if index % 2 else 0)
            if spike_after and stamp >= spike_after:
                price *= 10
            for instrument_index, instrument in enumerate(instruments):
                if stamp in (missing_by_instrument or {}).get(instrument.key, ()):
                    continue
                symbol_price = price + instrument_index
                rows.append(dict(instrument_id=instrument.key, trading_date=stamp.date(), bar_start=stamp,
                    bar_end=stamp + timedelta(minutes=1), open=symbol_price, high=symbol_price + .02, low=symbol_price - .02,
                    close=symbol_price + .005, volume=100 + index, amount=(100 + index) * symbol_price))
    source = SourceRecord(provider="fixture", interface="synthetic", upstream="synthetic minute fixture", downloaded_at=end,
        actual_mode="raw", limitations=("synthetic",), units={"volume": "share", "amount": "CNY"})
    requirement = DataRequirement(requests=(query, calendar_request))
    store, catalog = MarketStore(root), MarketCatalog(root)
    fragments = store.put_chunk(NormalizedChunk(query, pd.DataFrame(rows), source, b"synthetic bars"))
    calendar = pd.DataFrame([dict(trading_date=begin.date(), session_start=begin, session_end=finish) for begin, finish in sessions])
    fragments += store.put_chunk(NormalizedChunk(calendar_request, calendar, source, b"synthetic calendar"))
    coverage = CoverageReport(entries=tuple(CoverageEntry(request=request) for request in requirement.requests), limitations=("synthetic",))
    ref = store.write_manifest(SnapshotManifest(requirement=requirement, fragments=fragments, coverage=coverage))
    catalog.create_request("fixture", requirement)
    catalog.commit_ready(("fixture",), ref, ())
    return dict(name="synthetic fixture", snapshot_id=ref.id, query=query.model_dump(mode="json"), frequency_minutes=1), start


def training_case(start, **updates):
    value = dict(name="fixture model", model_type="linear", train_start=start.isoformat(), train_end=(start + timedelta(minutes=35)).isoformat(),
        validation_start=(start + timedelta(minutes=35)).isoformat(), validation_end=(start + timedelta(minutes=60)).isoformat(),
        test_start=(start + timedelta(minutes=60)).isoformat(), test_end=(start + timedelta(minutes=90)).isoformat(),
        lookback=4, horizon=2, epochs=2, batch_size=8, learning_rate=.001, hidden_size=8, layers=1, seed=7, device="cpu", patience=3)
    value.update(updates)
    return value
