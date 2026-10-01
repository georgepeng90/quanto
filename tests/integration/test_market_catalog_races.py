import multiprocessing

import pytest

from quanto.data.contracts import MarketDataError
from tests.fixtures.market_data_cases import market_case


def contender(root, owner, start, finish, output):
    from quanto.data.catalog import MarketCatalog
    start.wait(10)
    catalog = MarketCatalog(root)
    token = catalog.acquire_lease("series", owner, 20)
    output.put(bool(token))
    finish.wait(10)
    if token:
        catalog.release_lease(token)


def test_processes_contend_for_same_series_lease(tmp_path):
    from quanto.data.catalog import MarketCatalog
    MarketCatalog(tmp_path)
    context = multiprocessing.get_context("spawn")
    start, finish, output = context.Event(), context.Event(), context.Queue()
    children = [context.Process(target=contender, args=(tmp_path, str(index), start, finish, output)) for index in range(2)]
    try:
        for child in children:
            child.start()
        start.set()
        results = [output.get(timeout=15) for _ in children]
        assert sorted(results) == [False, True]
    finally:
        finish.set()
        for child in children:
            child.join(15)
            if child.is_alive():
                child.kill()
                child.join()


def test_expired_owner_cannot_publish_after_new_lease(tmp_path):
    from quanto.data.catalog import MarketCatalog
    now = [100.]
    first = MarketCatalog(tmp_path, clock=lambda: now[0])
    second = MarketCatalog(tmp_path, clock=lambda: now[0])
    old = first.acquire_lease("series", "old", 10)
    now[0] = 111.
    current = second.acquire_lease("series", "new", 10)
    assert current.generation > old.generation
    with pytest.raises(MarketDataError):
        first.renew_lease(old, 10)
    from quanto.data.contracts import DataRequirement
    from quanto.data.storage import MarketStore
    from tests.unit.test_market_storage import chunk, manifest
    store = MarketStore(tmp_path)
    content = chunk()
    first.create_request("stale", DataRequirement(requests=(content.request,)))
    snapshot = store.write_manifest(manifest(store.put_chunk(content)))
    with pytest.raises(MarketDataError):
        first.commit_ready(("stale",), snapshot, (old,))


def test_cancel_committed_before_ready_wins(tmp_path):
    from quanto.data.catalog import MarketCatalog
    from quanto.data.storage import MarketStore
    from tests.unit.test_market_storage import chunk, manifest
    catalog, store = MarketCatalog(tmp_path), MarketStore(tmp_path)
    requirement = market_case("1d")[0]
    catalog.create_request("cancelled", requirement)
    token = catalog.acquire_lease("series", "owner", 10)
    ref = store.write_manifest(manifest(store.put_chunk(chunk())))
    catalog.cancel_request("cancelled")
    assert catalog.commit_ready(("cancelled",), ref, (token,)) == ()
    with pytest.raises(MarketDataError):
        catalog.resolve_snapshot(ref.id)


def test_interrupted_publish_leaves_no_ready_reference(tmp_path, monkeypatch):
    from tests.unit.test_market_acquisition import FixtureProvider, coordinator, prepare
    provider = FixtureProvider()
    engine, catalog = coordinator(tmp_path, (provider,))
    def interrupt(*args):
        raise OSError("interrupted")
    monkeypatch.setattr(engine.store, "write_manifest", interrupt)
    with pytest.raises((MarketDataError, OSError)):
        prepare(engine, catalog, provider.requirement)
    with catalog.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
