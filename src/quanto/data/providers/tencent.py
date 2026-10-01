from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from quanto.data.contracts import BAR_COLUMNS, DataRequest, MarketDataError, NormalizedChunk, PriceBasis, SourceRecord, content_key
from quanto.data.providers.base import FetchContext, ProviderCapabilities, daily_bars, local_date_bounds
from quanto.data.validation import validate_chunk


URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"


def transport(params: dict, timeout: float, url: str | None = None) -> bytes:
    request = Request((url or URL) + "?" + urlencode(params), headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


class TencentProvider:
    name = "tencent"

    def __init__(self, transport=transport, unit_evidence: dict | None = None):
        self.transport, self.unit_evidence = transport, unit_evidence
        if unit_evidence and not unit_evidence.get("evidence"):
            raise ValueError("unit conversion requires evidence")

    def capabilities(self):
        return ProviderCapabilities(("bars",), ("1d",), {"etf": BAR_COLUMNS[4:], "index": BAR_COLUMNS[4:]}, exchanges=("SSE", "SZSE"), currencies=("CNY",))

    def fetch(self, request: DataRequest, context: FetchContext) -> NormalizedChunk:
        if not self.capabilities().supports(request) or len(request.instruments) != 1:
            raise MarketDataError("CAPABILITY_UNAVAILABLE", "Tencent supports one daily instrument per fetch")
        instrument = request.instruments[0]
        symbol = ("sz" if instrument.exchange == "SZSE" else "sh") + instrument.code
        records, modes, evidence, parameters = [], set(), [], []
        start_day, end_day = local_date_bounds(request)
        try:
            for year in range(start_day.year, end_day.year + 1, 2):
                params = {"_var": f"kline_dayqfq{year}", "param": f"{symbol},day,{year}-01-01,{year+1}-12-31,640,qfq"}
                if self.transport is transport:
                    from quanto.data.providers.sdk_call import run_http_call
                    raw = run_http_call(params, context)
                else:
                    raw = self.transport(params, context.remaining())
                context.remaining()
                parsed = json.loads(raw.decode().split("=", 1)[-1].strip().rstrip(";"))
                if parsed.get("code") != 0:
                    raise ValueError("Tencent provider error")
                payload = parsed.get("data", {}).get(symbol, {})
                mode = "qfqday" if "qfqday" in payload else "day"
                rows = payload.get(mode)
                if not isinstance(rows, list):
                    raise ValueError("missing day/qfqday list")
                modes.add(mode)
                evidence.append(raw)
                parameters.append(params)
                for row in rows:
                    if not isinstance(row, list) or len(row) < 9:
                        raise ValueError("short Tencent row")
                    records.append(dict(date=row[0], open=float(row[1]), close=float(row[2]), high=float(row[3]), low=float(row[4]), volume=float(row[5]), amount=float(row[8])))
            if len(modes) > 1:
                raise MarketDataError("SEMANTIC_CONFLICT", "Tencent windows returned different price modes")
            frame = pd.DataFrame(records, columns=("date", "open", "close", "high", "low", "volume", "amount"))
            units, verified = {"volume": "unknown", "amount": "unknown"}, ()
            limitations = ["unverified_price_basis", "unknown_availability"]
            if self.unit_evidence:
                if self.unit_evidence.get("volume") == "lot100":
                    frame["volume"] *= 100
                    units["volume"] = "share"
                if self.unit_evidence.get("amount") == "ten_thousand_CNY":
                    frame["amount"] *= 10000
                    units["amount"] = "CNY"
                if "unknown" not in units.values():
                    verified = ("units",)
            if "unknown" in units.values():
                limitations.append("unknown_units")
            mode = next(iter(modes))
            basis_id = content_key([self.name, mode, "unverified_anchor", units, self.unit_evidence])
            resolved = request.model_copy(update={"price_basis": PriceBasis(mode="vendor_research", basis_id=basis_id)})
            frame = daily_bars(frame, instrument, request)
            source = SourceRecord(provider=self.name, upstream="Tencent", interface=URL, parameters={"windows": parameters, "unit_evidence": self.unit_evidence},
                downloaded_at=datetime.now(timezone.utc), actual_mode=mode, units=units, verified=verified, limitations=tuple(limitations))
            return validate_chunk(NormalizedChunk(resolved, frame, source, b"\n".join(evidence)))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise MarketDataError("FAILED", str(exc)) from exc
