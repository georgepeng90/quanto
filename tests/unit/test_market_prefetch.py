import threading

import pytest

from quanto.data.contracts import DataQuery, MarketDataError
from tests.unit.test_market_acquisition import FixtureProvider
from tests.fixtures.market_data_cases import import_case


class SlowProvider(FixtureProvider):
    def __init__(self):
        super().__init__()
        self.entered, self.release = threading.Event(), threading.Event()

    def fetch(self, request, context):
        self.entered.set()
        assert self.release.wait(10)
        return super().fetch(request, context)


def test_one_wait_timeout_does_not_cancel_request(tmp_path):
    from quanto.data import MarketData
    provider = SlowProvider()
    try:
        with MarketData(tmp_path, providers=(provider,)) as market:
            task = market.prefetch(provider.requirement)
            assert provider.entered.wait(5)
            with pytest.raises(MarketDataError) as failure:
                market.wait_ready(task.id, timeout=0)
            assert failure.value.code == "WAIT_TIMEOUT"
            assert market.get_prefetch(task.id).state == "running"
            provider.release.set()
            assert market.wait_ready(task.id, 5).coverage.complete
    finally:
        provider.release.set()


def test_identical_requests_share_fetch_but_cancel_independently(tmp_path):
    from quanto.data import MarketData
    provider = SlowProvider()
    try:
        with MarketData(tmp_path, providers=(provider,)) as market:
            first = market.prefetch(provider.requirement)
            assert provider.entered.wait(5)
            second = market.prefetch(provider.requirement)
            assert first.id != second.id
            market.cancel_prefetch(first.id)
            provider.release.set()
            assert market.wait_ready(second.id, 5).coverage.complete
            assert market.get_prefetch(first.id).state == "cancelled"
            assert len(provider.calls) == 3
    finally:
        provider.release.set()


def test_all_request_cancellation_blocks_late_ready(tmp_path):
    from quanto.data import MarketData
    provider = SlowProvider()
    try:
        with MarketData(tmp_path, providers=(provider,)) as market:
            task = market.prefetch(provider.requirement)
            assert provider.entered.wait(5)
            market.cancel_prefetch(task.id)
            provider.release.set()
            with pytest.raises(MarketDataError) as failure:
                market.wait_ready(task.id, 5)
            assert failure.value.code == "CANCELLED"
        assert market.get_prefetch(task.id).snapshot is None
    finally:
        provider.release.set()


def test_ready_then_cancel_does_not_unpublish_snapshot(tmp_path):
    from quanto.data import MarketData
    _, _, requirement = import_case(tmp_path)
    with MarketData(tmp_path, providers=()) as market:
        task = market.prefetch(requirement)
        snapshot = market.wait_ready(task.id, 5)
        market.cancel_prefetch(task.id)
        query = DataQuery(**requirement.requests[0].model_dump())
        assert len(market.read_bars(snapshot, query)) == 8
        assert market.get_prefetch(task.id).state == "ready"
    with MarketData(tmp_path, providers=()) as reopened:
        assert len(reopened.read_bars(snapshot, query)) == 8
        with pytest.raises(MarketDataError) as failure:
            reopened.get_prefetch(task.id)
        assert failure.value.code == "TASK_NOT_FOUND"


def test_unpublished_snapshot_is_not_publicly_readable(tmp_path):
    from quanto.data import MarketData
    from quanto.data.coverage import evaluate_coverage
    from quanto.data.contracts import SnapshotManifest
    store, catalog, requirement = import_case(tmp_path)
    fragments = catalog.fragments(requirement)
    snapshot = store.write_manifest(SnapshotManifest(requirement=requirement, fragments=fragments, coverage=evaluate_coverage(requirement, fragments, store)))
    with MarketData(tmp_path, providers=()) as market:
        with pytest.raises(MarketDataError) as failure:
            market.read_bars(snapshot, DataQuery(**requirement.requests[0].model_dump()))
        assert failure.value.code == "DATA_NOT_PREPARED"


def test_late_consumer_binding_error_is_terminal_not_stuck(tmp_path, monkeypatch):
    from quanto.data import MarketData
    provider = FixtureProvider()
    published, release = threading.Event(), threading.Event()
    with MarketData(tmp_path, providers=(provider,)) as market:
        original = market.catalog.commit_ready
        count = [0]
        def commit(*args):
            count[0] += 1
            if count[0] == 1:
                result = original(*args)
                published.set()
                assert release.wait(5)
                return result
            raise OSError("binding storage error")
        monkeypatch.setattr(market.catalog, "commit_ready", commit)
        first = market.prefetch(provider.requirement)
        try:
            assert published.wait(5)
            second = market.prefetch(provider.requirement)
        finally:
            release.set()
        assert market.wait_ready(first.id, 5).coverage.complete
        with pytest.raises(MarketDataError) as failure:
            market.wait_ready(second.id, 1)
        assert failure.value.code == "FAILED"


def test_different_instruments_can_resolve_distinct_research_bases(tmp_path):
    from quanto.data import MarketData
    from quanto.data.contracts import DataRequirement, DataQuery, PriceBasis
    from quanto.data.imports import import_table
    from tests.fixtures.market_data_cases import market_case
    requirement, bars, calendar = market_case("1d")
    wanted = bars.request.model_copy(update={"price_basis": PriceBasis(mode="vendor_research")})
    requirement = DataRequirement(requests=(wanted, calendar.request))
    with MarketData(tmp_path, providers=()) as market:
        import_table(market.store, market.catalog, calendar.request, calendar.frame, calendar.source, calendar.evidence)
        for index, instrument in enumerate(wanted.instruments):
            request = wanted.model_copy(update={"instruments": (instrument,), "price_basis": PriceBasis(mode="vendor_research", basis_id=f"source_{index}")})
            frame = bars.frame.loc[bars.frame.instrument_id == instrument.key]
            import_table(market.store, market.catalog, request, frame, bars.source, bars.evidence)
        snapshot = market.wait_ready(market.prefetch(requirement).id, 10)
        assert snapshot.coverage.complete
        for index, instrument in enumerate(wanted.instruments):
            query = DataQuery(**wanted.model_copy(update={"instruments": (instrument,), "price_basis": PriceBasis(mode="vendor_research", basis_id=f"source_{index}")}).model_dump())
            assert len(market.read_bars(snapshot, query)) == 2
