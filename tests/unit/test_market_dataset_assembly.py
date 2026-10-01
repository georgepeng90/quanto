import pandas as pd
import pytest

from quanto.backtest.market_dataset import assemble_dataset, build_data_requirement
from quanto.data.contracts import MarketDataError
from tests.fixtures.market_dataset_cases import research_market


PAYLOAD = {"symbols": ["000001"], "start_date": "2024-01-01", "end_date": "2024-01-05"}


def test_market_assembly_preserves_full_history_and_next_session(tmp_path):
    requirement = build_data_requirement(PAYLOAD)
    with research_market(tmp_path) as market:
        ref = market.wait_ready(market.prefetch(requirement).id, 10)
        bundle = assemble_dataset(market, ref, requirement, {})
        assert list(bundle.prices.date) == list(pd.bdate_range("2024-01-01", "2024-01-05"))
        assert (bundle.calendar.date > bundle.prices.date.max()).any()
        assert bundle.price_mode == "adjusted_research"
        assert any("研究模型假设" in item for item in bundle.manifest.limitations)
        assert not any(item.request.kind == "bars" for item in market.catalog.fragments(build_data_requirement({**PAYLOAD, "price_mode": "raw_cash"})))
        assert all(item.request.kind in {"bars", "calendar"} for item in market.catalog.fragments(requirement))
        assert ref.quality["series"]


def test_raw_cash_assembly_requires_factual_reference_tables(tmp_path):
    requirement = build_data_requirement(PAYLOAD)
    with research_market(tmp_path) as market:
        ref = market.wait_ready(market.prefetch(requirement).id, 10)
        with pytest.raises((ValueError, MarketDataError), match="raw_cash"):
            assemble_dataset(market, ref, requirement, {"price_mode": "raw_cash"})


def test_consumer_requirement_selects_full_history_and_auxiliary_visibility():
    req = build_data_requirement({**PAYLOAD, "historical_start": "2023-01-01", "auxiliary": {
        "signals": {"columns": ["available_at", "symbol", "score"]}}})
    assert req.requests[0].start.year == 2023
    assert next(item for item in req.requests if item.kind == "auxiliary:signals").columns == ("available_at", "symbol", "score")


@pytest.mark.parametrize("split_status", [False, True])
def test_offline_factual_assembly_matches_daily_tables_orders_and_reports(tmp_path, split_status):
    from quanto.data import MarketData
    from quanto.data.contracts import SourceRecord, DataRequirement
    from quanto.data.imports import import_table
    from quanto.data.providers.base import daily_bars
    from quanto.backtest.dataset_loaders import load_dataset
    from quanto.backtest.engine import run_backtest
    from quanto.reports.backtest_report import build_report
    from quanto.strategies.buy_hold import BuyHold
    from tests.fixtures.backtest_cases import dataset_case
    from tests.fixtures.engine_cases import spec_case
    from pandas.testing import assert_frame_equal

    source_dir = dataset_case(tmp_path / "old")
    events = pd.DataFrame([dict(symbol="000001", event_type="dividend", record_date="2024-01-01",
        ex_date="2024-01-02", pay_date="2024-01-03", cash_per_share=.01, split_ratio=None)])
    events.to_csv(source_dir / "events.csv", index=False)
    old = load_dataset(source_dir)
    requirement = build_data_requirement({**PAYLOAD, "end_date": "2024-01-03", "price_mode": "raw_cash",
        "auxiliary": {"signals": {"columns": ["available_at", "symbol", "score"]}}})
    requirement = DataRequirement(requests=tuple(request.model_copy(update={"end": pd.Timestamp("2024-01-06", tz="UTC").to_pydatetime()})
        if request.kind == "calendar" else request for request in requirement.requests), quality=requirement.quality)
    source = SourceRecord(provider="fixture", upstream="synthetic", interface="known_facts", actual_mode="raw",
        downloaded_at=pd.Timestamp("2024-01-06", tz="UTC").to_pydatetime(), units={"volume": "share", "amount": "CNY"},
        verified=("units", "price_basis", "calendar", "instruments", "status", "actions", "auxiliary:signals"))
    frames = {}
    for request in requirement.requests:
        if request.kind == "bars":
            instrument = request.instruments[0]
            frame = old.prices if instrument.instrument_type == "etf" else old.benchmark.rename(columns={"turnover": "amount"})
            frames[request.series_key] = daily_bars(frame, instrument, request)
        elif request.kind == "calendar":
            days = old.calendar.date.dt.tz_localize("Asia/Shanghai")
            frames[request.series_key] = pd.DataFrame(dict(trading_date=old.calendar.date.dt.date,
                session_start=(days + pd.Timedelta(hours=9, minutes=30)).dt.tz_convert("UTC"),
                session_end=(days + pd.Timedelta(hours=15)).dt.tz_convert("UTC")))
        elif request.kind.startswith("auxiliary:"):
            frames[request.series_key] = pd.DataFrame(dict(available_at=[pd.Timestamp("2024-01-02T10:00:00Z")], symbol=["000001"], score=[1]))
        else:
            frame = {"instruments": old.instruments, "trading_status": old.status, "corporate_actions": old.events}[request.kind]
            frames[request.series_key] = frame.rename(columns={"symbol": "instrument_id", "date": "trading_date"}).assign(instrument_id="SZSE:etf:000001")
    with MarketData(tmp_path / "market", providers=()) as market:
        for request in requirement.requests:
            if split_status and request.kind == "trading_status":
                for omitted in ("can_buy", "can_sell"):
                    partial = request.model_copy(update={"columns": tuple(column for column in request.columns if column != omitted)})
                    import_table(market.store, market.catalog, partial, frames[request.series_key].drop(columns=omitted), source, omitted.encode())
            else:
                import_table(market.store, market.catalog, request, frames[request.series_key], source, b"declared synthetic facts")
        ref = market.wait_ready(market.prefetch(requirement).id, 10)
        new = assemble_dataset(market, ref, requirement, {"universe_rule": old.manifest.universe_rule})
    for name in ("prices", "benchmark", "calendar", "instruments", "status", "events", "eligibility"):
        assert_frame_equal(getattr(new, name), getattr(old, name), check_dtype=False)
    assert new.auxiliary["signals"].available_at.iloc[0] == pd.Timestamp("2024-01-02T10:00:00Z")
    spec = spec_case(old, start="2024-01-02", end="2024-01-03")
    expected = run_backtest(old, BuyHold(), spec, lambda _: None, lambda: False)
    actual = run_backtest(new, BuyHold(), spec, lambda _: None, lambda: False)
    assert_frame_equal(actual.orders, expected.orders)
    assert build_report(actual, new, spec).metrics == build_report(expected, old, spec).metrics
