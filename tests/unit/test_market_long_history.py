from quanto.data import MarketData
from quanto.data.contracts import DataQuery
from quanto.data.imports import import_table
from scripts.benchmark_market_data import synthetic_case


def test_long_history_complete_local_prefetch_is_bounded_and_offline(tmp_path):
    _, requirement, chunks = synthetic_case(32, 2520)
    with MarketData(tmp_path, providers=()) as market:
        for chunk in chunks:
            import_table(market.store, market.catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
        ref = market.wait_ready(market.prefetch(requirement).id, timeout=10)
        request = requirement.requests[0]
        query = DataQuery(**request.model_copy(update={"instruments": request.instruments[:1],
            "price_basis": chunks[0].request.price_basis}).model_dump())
        assert len(market.read_bars(ref, query)) == 2520
        assert ref.coverage.complete
