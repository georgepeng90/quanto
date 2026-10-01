import asyncio

import pytest

from quanto.data.contracts import DataQuery, MarketDataError
from tests.fixtures.market_data_cases import import_case
from tests.unit.test_market_prefetch import SlowProvider


def test_async_slow_source_keeps_event_loop_responsive(tmp_path):
    from quanto.data import MarketData
    provider = SlowProvider()
    async def exercise():
        async with MarketData(tmp_path, providers=(provider,)) as market:
            task = await market.prefetch_async(provider.requirement)
            while not provider.entered.is_set():
                await asyncio.sleep(.01)
            waiter = asyncio.create_task(market.wait_ready_async(task.id))
            await asyncio.sleep(0)
            assert not waiter.done()
            assert (await market.get_prefetch_async(task.id)).state == "running"
            provider.release.set()
            assert (await asyncio.wait_for(waiter, 5)).coverage.complete
    try:
        asyncio.run(exercise())
    finally:
        provider.release.set()


def test_cancelled_async_wait_keeps_prefetch_alive(tmp_path):
    from quanto.data import MarketData
    provider = SlowProvider()
    async def exercise():
        async with MarketData(tmp_path, providers=(provider,)) as market:
            task = await market.prefetch_async(provider.requirement)
            waiter = asyncio.create_task(market.wait_ready_async(task.id))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert (await market.get_prefetch_async(task.id)).state in {"queued", "running"}
            provider.release.set()
            assert (await market.wait_ready_async(task.id, 5)).coverage.complete
    try:
        asyncio.run(exercise())
    finally:
        provider.release.set()


def test_async_batches_have_bounded_backpressure(tmp_path):
    from quanto.data import MarketConfig, MarketData
    _, _, requirement = import_case(tmp_path)
    async def exercise():
        async with MarketData(tmp_path, MarketConfig(batch_size=2), providers=()) as market:
            task = await market.prefetch_async(requirement)
            snapshot = await market.wait_ready_async(task.id, 5)
            query = DataQuery(**requirement.requests[0].model_dump())
            count = 0
            async for batch in market.iter_batches_async(snapshot, query):
                assert batch.num_rows <= 2
                count += batch.num_rows
                await asyncio.sleep(0)
            assert count == 8
    asyncio.run(exercise())


def test_sync_async_results_match(tmp_path):
    from quanto.data import MarketData
    from pandas.testing import assert_frame_equal
    _, _, requirement = import_case(tmp_path)
    async def exercise():
        async with MarketData(tmp_path, providers=()) as market:
            assert market.coverage(requirement) == await market.coverage_async(requirement)
            task = market.prefetch(requirement)
            snapshot = await market.wait_ready_async(task.id, 5)
            query = DataQuery(**requirement.requests[0].model_dump())
            assert_frame_equal(market.read_bars(snapshot, query), await market.read_bars_async(snapshot, query))
    asyncio.run(exercise())


def test_async_iterator_can_be_closed_after_instance_shutdown(tmp_path):
    from quanto.data import MarketData
    _, _, requirement = import_case(tmp_path)
    async def exercise():
        async with MarketData(tmp_path, providers=()) as market:
            task = await market.prefetch_async(requirement)
            snapshot = await market.wait_ready_async(task.id, 5)
            iterator = market.iter_batches_async(snapshot, DataQuery(**requirement.requests[0].model_dump()))
            assert (await anext(iterator)).num_rows == 8
        await iterator.aclose()
    asyncio.run(exercise())
