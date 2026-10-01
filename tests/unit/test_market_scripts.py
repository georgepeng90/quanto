import hashlib
from quanto.data.contracts import PriceBasis, SourceRecord
from scripts.import_market_data import import_file
from scripts.prefetch_market_data import prepare
from scripts.prepare_strategy_research_data import prepare_from_snapshot
from tests.fixtures.market_data_cases import market_case
from tests.fixtures.market_dataset_cases import research_market
from quanto.backtest.market_dataset import build_data_requirement


def test_legacy_import_keeps_hash_units_and_unknowns(tmp_path):
    requirement, bars, calendar = market_case("1d")
    request = bars.request.model_copy(update={"instruments": bars.request.instruments[:1],
        "price_basis": PriceBasis(mode="vendor_research", basis_id="legacy_unknown")})
    source = bars.frame[bars.frame.instrument_id == request.instruments[0].key].rename(columns={"trading_date": "date"})
    source = source[["date", "open", "high", "low", "close", "volume", "amount"]]
    path = tmp_path / "tencent-old" / "000001.csv"
    path.parent.mkdir()
    source.to_csv(path, index=False)
    original = path.read_bytes()
    metadata = {"request": request.model_dump(mode="json"), "daily_date_column": "date",
        "source": SourceRecord(provider="legacy_import", upstream="unknown", interface="saved_csv", actual_mode="unknown",
            downloaded_at=request.end, units={"volume": "unknown", "amount": "unknown"},
            limitations=("unknown_units", "unverified_price_basis", "unknown_availability")).model_dump(mode="json")}
    refs = import_file(tmp_path / "market", path, metadata)
    assert refs[0].source.parameters["input_sha256"] == hashlib.sha256(original).hexdigest()
    assert refs[0].source.units["volume"] == "unknown"
    assert "unverified_price_basis" in refs[0].source.limitations
    assert path.read_bytes() == original
    assert not (tmp_path / "market" / "tencent").exists()


def test_explicit_code_column_mapping_preserves_leading_zeroes(tmp_path):
    requirement, bars, _ = market_case()
    request = bars.request.model_copy(update={"instruments": bars.request.instruments[:1]})
    frame = bars.frame.loc[bars.frame.instrument_id == request.instruments[0].key].copy()
    frame["instrument_id"] = "000001"
    path = tmp_path / "legacy-minute.csv"
    frame.rename(columns={"instrument_id": "code"}).to_csv(path, index=False)
    metadata = {"request": request.model_dump(mode="json"), "source": bars.source.model_dump(mode="json"),
        "column_map": {"code": "instrument_id"}, "instrument_map": {"000001": request.instruments[0].key}}
    refs = import_file(tmp_path / "market", path, metadata)
    assert sum(ref.row_count for ref in refs) == 4
    assert all(ref.request.instruments == request.instruments for ref in refs)


def test_prefetch_script_reuses_prepared_cache_offline(tmp_path):
    from tests.fixtures.market_data_cases import import_case
    _, _, req = import_case(tmp_path)
    result = prepare(tmp_path, req.model_dump(mode="json"), providers=())
    assert result["state"] == "ready"
    assert prepare(tmp_path, req.model_dump(mode="json"), providers=())["snapshot"] == result["snapshot"]


def test_research_acquisition_uses_module_and_never_vendor_paths(tmp_path):
    requirement = build_data_requirement(dict(symbols=["000001"], start_date="2024-01-01", end_date="2024-01-05"))
    root = tmp_path / "market"
    with research_market(root) as market:
        ref = market.wait_ready(market.prefetch(requirement).id, 10)
    result = prepare_from_snapshot(root, ref.id, requirement, tmp_path / "research", {})
    assert result["market_snapshot_ref"]["id"] == ref.id
    assert (tmp_path / "research" / "dataset" / "prices.parquet").exists()
    assert not list(tmp_path.rglob("raw/tencent-*"))
    assert not (root / "etf").exists()


def test_minute_measurement_fetches_only_last_missing_minute_and_reuses():
    import pandas as pd
    from scripts.benchmark_market_data import measure
    result = measure(2, 3, "1m")
    assert result["etf_rows"] == 2 * 3 * 240
    assert result["gap_request_count"] == 3  # Two ETFs and one benchmark.
    assert result["repeat_request_count"] == 0
    assert all(pd.Timestamp(request["end"]) - pd.Timestamp(request["start"]) == pd.Timedelta(minutes=1) for request in result["gap_requests"])
    assert result["object_bytes"] > 0
    assert result["batch_rss_peak"] >= result["batch_rss_baseline"]


def test_daily_measurement_isolates_shared_library_in_worker():
    from quanto.data import get_market_root
    from scripts.benchmark_market_data import measure
    configured = get_market_root()
    result = measure(2, 3, "1d")
    assert result["seconds"]["workbench_job"] is not None
    assert result["repeat_request_count"] == 0
    assert get_market_root() == configured
    assert not configured.exists()
