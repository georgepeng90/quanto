from datetime import datetime, timezone

import pandas as pd
import pytest

from quanto.data.contracts import CoverageEntry, CoverageReport, DataRequest, DataRequirement, InstrumentId, MarketDataError, NormalizedChunk, PriceBasis, SnapshotManifest, SourceRecord


def chunk(value=10):
    instrument = InstrumentId(exchange="SZSE", instrument_type="etf", code="000001")
    request = DataRequest(kind="bars", instruments=(instrument,), start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                          end=datetime(2024, 1, 3, tzinfo=timezone.utc), columns=("close",), frequency="1d", price_basis=PriceBasis(mode="raw"))
    starts = pd.to_datetime(["2024-01-01T01:30:00Z", "2024-01-02T01:30:00Z"])
    frame = pd.DataFrame(dict(instrument_id=[instrument.key] * 2, trading_date=[day.date() for day in starts], bar_start=starts,
                              bar_end=starts + pd.Timedelta(hours=5, minutes=30), open=value, high=value, low=value, close=value, volume=100, amount=1000))
    source = SourceRecord(provider="fixture", interface="daily", upstream="synthetic", downloaded_at=request.end, actual_mode="raw")
    return NormalizedChunk(request, frame, source, b"fixed source evidence")


def manifest(fragments):
    request = fragments[0].request
    return SnapshotManifest(requirement=DataRequirement(requests=(request,)), fragments=fragments,
                            coverage=CoverageReport(entries=(CoverageEntry(request=request),)))


def test_identical_manifest_reuses_id_and_fragment(tmp_path):
    from quanto.data.storage import MarketStore

    store = MarketStore(tmp_path)
    first = store.write_manifest(manifest(store.put_chunk(chunk())))
    second = store.write_manifest(manifest(store.put_chunk(chunk())))
    assert first == second
    assert len(list(tmp_path.glob("objects/*/*.parquet"))) == 1
    assert store.verify_snapshot(first).fragments[0].row_count == 2


def test_old_snapshot_bytes_survive_refresh(tmp_path):
    from quanto.data.storage import MarketStore

    store = MarketStore(tmp_path)
    first = store.write_manifest(manifest(store.put_chunk(chunk())))
    before = {path: path.read_bytes() for path in tmp_path.glob("objects/*/*.parquet")}
    second = store.write_manifest(manifest(store.put_chunk(chunk(11))))
    assert first.id != second.id
    assert all(path.read_bytes() == value for path, value in before.items())
    store.verify_snapshot(first)


@pytest.mark.parametrize("damage", ["tampered", "missing", "extra", "symlink"])
def test_tampered_missing_extra_or_symlink_is_rejected(tmp_path, damage):
    from quanto.data.storage import MarketStore

    store = MarketStore(tmp_path)
    snapshot = store.write_manifest(manifest(store.put_chunk(chunk())))
    target = next(tmp_path.glob("objects/*/*.parquet"))
    if damage == "tampered":
        target.write_bytes(b"changed")
    elif damage == "missing":
        target.unlink()
    elif damage == "extra":
        (tmp_path / "snapshots" / snapshot.id / "extra.txt").write_text("extra")
    else:
        other = tmp_path / "outside.parquet"
        target.replace(other)
        try:
            target.symlink_to(other)
        except OSError:
            pytest.skip("symlink privilege unavailable")
    with pytest.raises(MarketDataError) as failure:
        store.verify_snapshot(snapshot)
    assert failure.value.code == "INTEGRITY_ERROR"


def test_unpublished_manifest_is_not_resolvable(tmp_path):
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore

    store, catalog = MarketStore(tmp_path), MarketCatalog(tmp_path)
    fragments = store.put_chunk(chunk())
    catalog.register_fragments(fragments)
    assert catalog.fragments(DataRequirement(requests=(chunk().request,))) == fragments
    snapshot = store.write_manifest(manifest(fragments))
    with pytest.raises(MarketDataError):
        catalog.resolve_snapshot(snapshot.id)
