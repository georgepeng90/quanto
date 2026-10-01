from dataclasses import replace

import pandas as pd
import pytest

from quanto.data import MarketData
from quanto.data.contracts import MarketDataError
from quanto.data.imports import import_table
from quanto.data.validation import validate_chunk
from tests.fixtures.market_data_cases import market_case


def test_conflicting_units_within_instrument_block_ready(tmp_path):
    requirement, bars, calendar = market_case("1d")
    with MarketData(tmp_path, providers=()) as market:
        import_table(market.store, market.catalog, calendar.request, calendar.frame, calendar.source, calendar.evidence)
        for day, unit in (("2024-01-31", "share"), ("2024-02-01", "lot100")):
            frame = bars.frame.loc[bars.frame.trading_date == pd.Timestamp(day).date()]
            source = bars.source.model_copy(update={"units": {"volume": unit, "amount": "CNY"}, "verified": ("price_basis",)})
            import_table(market.store, market.catalog, bars.request, frame, source, day.encode())
        with pytest.raises(MarketDataError) as failure:
            market.wait_ready(market.prefetch(requirement).id, 10)
        assert failure.value.coverage.entries[0].reason == "SEMANTIC_CONFLICT"


@pytest.mark.parametrize("units", [{"volume": "lot100", "amount": "CNY"}, {"volume": "share", "amount": "USD"}])
def test_verified_units_must_be_normalized_to_declared_currency(units):
    _, bars, _ = market_case("1d")
    with pytest.raises(MarketDataError, match="verified units"):
        validate_chunk(replace(bars, source=bars.source.model_copy(update={"units": units})))
