from datetime import datetime, timezone

import pandas as pd

from quanto.data.contracts import DataRequest, DataRequirement, InstrumentId, NormalizedChunk, PriceBasis, SourceRecord


def market_case(frequency="1m"):
    instruments = tuple(InstrumentId(exchange="SZSE", instrument_type="etf", code=code) for code in ("000001", "000002"))
    start, end = datetime(2024, 1, 31, tzinfo=timezone.utc), datetime(2024, 2, 2, tzinfo=timezone.utc)
    request = DataRequest(kind="bars", instruments=instruments, start=start, end=end, columns=("open", "high", "low", "close", "volume", "amount"), frequency=frequency, price_basis=PriceBasis(mode="raw"))
    calendar_request = DataRequest(kind="calendar", start=start, end=end, columns=("trading_date", "session_start", "session_end"))
    rows, sessions = [], []
    for day in ("2024-01-31", "2024-02-01"):
        begin = pd.Timestamp(f"{day}T01:30:00Z")
        finish = begin + pd.Timedelta(minutes=2)
        sessions.append(dict(trading_date=begin.date(), session_start=begin, session_end=finish))
        for instrument in instruments:
            for stamp in ([begin] if frequency == "1d" else [begin, begin + pd.Timedelta(minutes=1)]):
                rows.append(dict(instrument_id=instrument.key, trading_date=stamp.date(), bar_start=stamp,
                    bar_end=finish if frequency == "1d" else stamp + pd.Timedelta(minutes=1), open=10., high=11., low=9., close=10., volume=100., amount=1000.))
    source = SourceRecord(provider="fixture", interface="observed", upstream="synthetic", downloaded_at=end, actual_mode="raw",
                          units={"volume": "share", "amount": "CNY"}, verified=("units", "price_basis", "calendar"))
    bars = NormalizedChunk(request, pd.DataFrame(rows).iloc[::-1].reset_index(drop=True), source, b"synthetic bars")
    calendar = NormalizedChunk(calendar_request, pd.DataFrame(sessions), source, b"synthetic calendar")
    return DataRequirement(requests=(request, calendar_request)), bars, calendar


def import_case(root, frequency="1m"):
    from quanto.data.catalog import MarketCatalog
    from quanto.data.imports import import_table
    from quanto.data.storage import MarketStore

    store, catalog = MarketStore(root), MarketCatalog(root)
    requirement, bars, calendar = market_case(frequency)
    for chunk in (bars, calendar):
        import_table(store, catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
    return store, catalog, requirement
