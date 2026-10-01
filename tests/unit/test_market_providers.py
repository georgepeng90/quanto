from datetime import datetime, timezone
import json
from pathlib import Path
import time

import pandas as pd
import pytest

from quanto.data.contracts import DataRequest, InstrumentId, MarketDataError, PriceBasis


def request(kind="etf", frequency="1d"):
    return DataRequest(kind="bars", instruments=(InstrumentId(exchange="SSE", instrument_type=kind, code="512580" if kind == "etf" else "000300"),),
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 1, 3, tzinfo=timezone.utc),
        columns=("close", "amount"), frequency=frequency, price_basis=PriceBasis(mode="vendor_research"))


def response(mode="day", rows=None, code=0):
    return ("callback=" + json.dumps({"code": code, "data": {"sh512580": {mode: rows if rows is not None else [["2024-01-02", "10", "10", "11", "9", "20", "x", "x", "3"]]}}})).encode()


def context():
    from quanto.data.providers.base import FetchContext
    return FetchContext(deadline=time.monotonic() + 10, cancelled=lambda: False)


def test_requested_qfq_does_not_relabel_actual_day():
    from quanto.data.providers.tencent import TencentProvider
    chunk = TencentProvider(transport=lambda *args: response()).fetch(request(), context())
    assert chunk.source.actual_mode == "day"
    assert "unverified_price_basis" in chunk.source.limitations
    assert chunk.request.price_basis.mode == "vendor_research"
    assert chunk.request.price_basis.basis_id


def test_saved_tencent_response_preserves_actual_observation():
    from quanto.data.providers.tencent import TencentProvider
    raw = Path("tests/fixtures/market_sources/tencent_saved_day.json").read_bytes()
    query = request().model_copy(update={"start": datetime(2023, 5, 17, tzinfo=timezone.utc), "end": datetime(2023, 5, 19, tzinfo=timezone.utc)})
    chunk = TencentProvider(transport=lambda *args: raw).fetch(query, context())
    assert chunk.source.actual_mode == "day"
    assert len(chunk.frame) == 2
    assert "units" not in chunk.source.verified


def test_tencent_verified_units_and_unknown_evidence_are_distinct():
    from quanto.data.providers.tencent import TencentProvider
    known = TencentProvider(transport=lambda *args: response("qfqday"), unit_evidence={"volume": "lot100", "amount": "ten_thousand_CNY", "evidence": "synthetic fixture profile"}).fetch(request(), context())
    unknown = TencentProvider(transport=lambda *args: response()).fetch(request(), context())
    assert known.frame.volume.iloc[0] == 2000
    assert known.frame.amount.iloc[0] == 30000
    assert "units" in known.source.verified
    assert unknown.frame.volume.iloc[0] == 20
    assert unknown.source.units["volume"] == "unknown"
    assert "units" not in unknown.source.verified


@pytest.mark.parametrize("raw", [response(code=1), response(rows=[["2024-01-01"]]), b"invalid-jsonp"])
def test_malformed_tencent_response_is_not_usable(raw):
    from quanto.data.providers.tencent import TencentProvider
    with pytest.raises(MarketDataError):
        TencentProvider(transport=lambda *args: raw).fetch(request(), context())


def test_akshare_index_does_not_fabricate_ohlc():
    from quanto.data.providers.akshare import AKShareProvider
    chunk = AKShareProvider(sdk_call=lambda *args: pd.DataFrame({"date": ["2024-01-02"], "close": [4000.], "amount": [100.]})).fetch(request("index"), context())
    assert chunk.frame.open.isna().all()
    assert chunk.frame.high.isna().all()
    assert chunk.source.upstream == "Eastmoney"


@pytest.mark.parametrize("start,end", [("2024-01-02T01:30:00Z", "2024-01-02T07:00:00Z"),
    ("2024-01-01T16:00:00Z", "2024-01-02T16:00:00Z")])
def test_akshare_daily_parameters_include_requested_close_in_local_timezone(start, end):
    from quanto.data.providers.akshare import AKShareProvider
    calls = []
    def sdk(function, kwargs, context):
        calls.append(kwargs)
        return pd.DataFrame({"日期": ["2024-01-02"], "收盘": [10.], "成交额": [100.]})
    query = request().model_copy(update={"start": pd.Timestamp(start).to_pydatetime(), "end": pd.Timestamp(end).to_pydatetime()})
    chunk = AKShareProvider(sdk_call=sdk).fetch(query, context())
    assert calls[0]["start_date"] == calls[0]["end_date"] == "20240102"
    assert len(chunk.frame) == 1


def test_calendar_horizon_uses_trading_dates_in_exchange_timezone():
    from quanto.data.providers.akshare import AKShareProvider
    query = DataRequest(kind="calendar", start=pd.Timestamp("2024-01-01T16:00:00Z").to_pydatetime(),
        end=pd.Timestamp("2024-01-02T16:00:00Z").to_pydatetime(), columns=("trading_date", "session_start", "session_end"))
    chunk = AKShareProvider(sdk_call=lambda *args: pd.DataFrame({"trade_date": ["2024-01-02"]})).fetch(query, context())
    assert len(chunk.frame) == 2


@pytest.mark.parametrize("field,value", [("exchange", "NYSE"), ("currency", "USD")])
def test_chinese_sources_reject_unsupported_market_semantics(field, value):
    from quanto.data.providers.akshare import AKShareProvider
    from quanto.data.providers.tencent import TencentProvider
    query = request()
    query = query.model_copy(update={"instruments": (query.instruments[0].model_copy(update={field: value}),)}) if field == "exchange" else query.model_copy(update={field: value})
    for provider in (AKShareProvider(), TencentProvider()):
        assert not provider.capabilities().supports(query)
        with pytest.raises(MarketDataError, match="CAPABILITY_UNAVAILABLE"):
            provider.fetch(query, context())


def test_minute_fetch_is_not_silently_daily():
    from quanto.data.providers.akshare import AKShareProvider
    from quanto.data.providers.tencent import TencentProvider
    for provider in (AKShareProvider(), TencentProvider()):
        with pytest.raises(MarketDataError) as failure:
            provider.fetch(request(frequency="1m"), context())
        assert failure.value.code == "CAPABILITY_UNAVAILABLE"


def test_transport_deadline_is_bounded():
    from quanto.data.providers.tencent import TencentProvider
    from quanto.data.providers.base import FetchContext
    with pytest.raises(MarketDataError):
        TencentProvider().fetch(request(), FetchContext(time.monotonic() - 1, lambda: False))


def test_sdk_timeout_reaps_child_without_publishing(monkeypatch):
    import subprocess
    import sys
    from quanto.data.providers.sdk_call import run_sdk_call
    from quanto.data.providers.base import FetchContext

    real_popen, children = subprocess.Popen, []
    def launch(*args, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(MarketDataError):
        run_sdk_call("tool_trade_date_hist_sina", {}, FetchContext(time.monotonic() + .15, lambda: False))
    assert children[0].poll() is not None
