from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from quanto.data.contracts import BAR_COLUMNS, DataRequest, MarketDataError, NormalizedChunk, PriceBasis, SourceRecord, content_key
from quanto.data.providers.base import FetchContext, ProviderCapabilities, daily_bars
from quanto.data.providers.sdk_call import run_sdk_call
from quanto.data.validation import validate_chunk


class AKShareProvider:
    name = "akshare"

    def __init__(self, sdk_call=run_sdk_call):
        self.sdk_call = sdk_call

    def capabilities(self):
        return ProviderCapabilities(("bars", "calendar"), ("1d",), {"etf": BAR_COLUMNS[4:], "index": ("close", "amount")})

    def fetch(self, request: DataRequest, context: FetchContext) -> NormalizedChunk:
        if not self.capabilities().supports(request):
            raise MarketDataError("CAPABILITY_UNAVAILABLE", "AKShare does not support requested fields/frequency/basis")
        context.remaining()
        if request.kind == "calendar":
            function, kwargs = "tool_trade_date_hist_sina", {}
            raw = self.sdk_call(function, kwargs, context)
            days = pd.to_datetime(raw["trade_date"]).dt.normalize()
            if days.empty or days.min().date() > request.start.date() or days.max().date() < (pd.Timestamp(request.end) - pd.Timedelta(days=1)).date():
                raise MarketDataError("INCOMPLETE", "calendar source does not establish requested horizon")
            local = days.dt.tz_localize("Asia/Shanghai")
            sessions = []
            for begin, finish in [(pd.Timedelta(hours=9, minutes=30), pd.Timedelta(hours=11, minutes=30)), (pd.Timedelta(hours=13), pd.Timedelta(hours=15))]:
                sessions.append(pd.DataFrame({"trading_date": days.dt.date, "session_start": (local + begin).dt.tz_convert("UTC"), "session_end": (local + finish).dt.tz_convert("UTC")}))
            frame = pd.concat(sessions, ignore_index=True)
            frame = frame[(frame.session_start >= request.start) & (frame.session_end <= request.end)]
            source = SourceRecord(provider=self.name, upstream="Sina", interface=function, parameters=kwargs,
                downloaded_at=datetime.now(timezone.utc), actual_mode="calendar", verified=("calendar",))
        else:
            if len(request.instruments) != 1:
                raise MarketDataError("CAPABILITY_UNAVAILABLE", "adapter fetch expects one instrument")
            instrument = request.instruments[0]
            if instrument.instrument_type == "etf":
                function, kwargs = "fund_etf_hist_em", dict(symbol=instrument.code, period="daily", start_date=request.start.strftime("%Y%m%d"),
                    end_date=(pd.Timestamp(request.end) - pd.Timedelta(days=1)).strftime("%Y%m%d"), adjust="qfq")
            else:
                function, kwargs = "stock_zh_index_daily_em", dict(symbol=("sz" if instrument.exchange == "SZSE" else "sh") + instrument.code)
            raw = self.sdk_call(function, kwargs, context)
            renamed = raw.rename(columns={"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount"})
            frame = daily_bars(renamed, instrument, request)
            actual_mode = "qfq" if instrument.instrument_type == "etf" else "index_daily"
            request = request.model_copy(update={"price_basis": PriceBasis(mode="vendor_research", basis_id=content_key([self.name, function, actual_mode, "unverified_anchor"]))})
            source = SourceRecord(provider=self.name, upstream="Eastmoney", interface=function, parameters=kwargs,
                downloaded_at=datetime.now(timezone.utc), actual_mode=actual_mode, units={"volume": "unknown", "amount": "unknown"},
                limitations=("unverified_price_basis", "unknown_units", "unknown_availability"))
        context.remaining()
        evidence = raw.to_json(orient="table", date_format="iso", force_ascii=False).encode()
        return validate_chunk(NormalizedChunk(request, frame, source, evidence))
