"""Daily backtest datasets assembled from prepared market snapshots."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from quanto.backtest.dataset_loaders import validate_dataset
from quanto.backtest.dataset_schemas import DatasetBundle, DatasetManifest, INSTRUMENT_COLUMNS
from quanto.data import MarketData
from quanto.data.contracts import DataQuery, DataRequest, DataRequirement, InstrumentId, MarketDataError, PriceBasis, QualityPolicy, SnapshotRef, TableQuery


EVENT_COLUMNS = ("symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio")
RESEARCH_LIMITATIONS = ["供应商价格口径仅用于研究，不模拟真实分红和拆分现金流。",
    "交易标志、每手数量和结算周期为研究模型假设，不是真实可交易状态证据。",
    "首次观测日期仅是研究覆盖边界，未核实真实上市日。"]


def build_data_requirement(payload: dict) -> DataRequirement:
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols or not all(isinstance(code, str) and len(code) == 6 and code.isdigit() for code in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("invalid ETF symbols")
    start_day = date.fromisoformat(payload.get("historical_start", payload["start_date"]))
    performance_start = date.fromisoformat(payload["start_date"])
    end_day = date.fromisoformat(payload["end_date"])
    if start_day > performance_start or performance_start > end_day:
        raise ValueError("invalid history or data range")
    start = datetime.combine(start_day, datetime.min.time(), timezone.utc)
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), timezone.utc)
    instruments = tuple(InstrumentId(exchange="SSE" if code.startswith(("5", "6")) else "SZSE", instrument_type="etf", code=code) for code in symbols)
    mode = payload.get("price_mode", "adjusted_research")
    if mode not in {"raw_cash", "adjusted_research"}:
        raise ValueError("invalid price mode")
    basis = PriceBasis(mode="raw" if mode == "raw_cash" else "vendor_research")
    requests = [DataRequest(kind="bars", instruments=instruments, start=start, end=end,
        columns=("open", "high", "low", "close", "volume", "amount"), frequency="1d", price_basis=basis),
        DataRequest(kind="bars", instruments=(InstrumentId(exchange="SSE", instrument_type="index", code="000300"),),
            start=start, end=end, columns=("close", "amount"), frequency="1d", price_basis=basis),
        DataRequest(kind="calendar", start=start, end=end + timedelta(days=14), columns=("trading_date", "session_start", "session_end"))]
    if mode == "raw_cash":
        for kind, columns in (("instruments", ("instrument_id", "listing_date", "lot_size", "settlement_sessions", "group")),
            ("trading_status", ("instrument_id", "trading_date", "status", "can_buy", "can_sell")),
            ("corporate_actions", ("instrument_id", *EVENT_COLUMNS[1:]))):
            requests.append(DataRequest(kind=kind, instruments=instruments, start=start, end=end, columns=columns))
    for name, metadata in payload.get("auxiliary", {}).items():
        requests.append(DataRequest(kind=f"auxiliary:{name}", start=start, end=end, columns=tuple(metadata["columns"])))
    quality = QualityPolicy(required_evidence=("units", "price_basis") if mode == "raw_cash" else (),
        accepted_limitations=() if mode == "raw_cash" else ("unverified_price_basis", "unknown_units", "unknown_availability"))
    return DataRequirement(requests=tuple(requests), quality=quality)


def assemble_dataset(market: MarketData, snapshot: SnapshotRef, requirement: DataRequirement, research_assumptions: dict) -> DatasetBundle:
    bars = [request for request in requirement.requests if request.kind == "bars"]
    if any(request.frequency != "1d" for request in bars):
        raise ValueError("daily backtest assembly requires 1d bars")
    price_request = next(request for request in bars if all(item.instrument_type == "etf" for item in request.instruments))
    raw = research_assumptions.get("price_mode", "raw_cash" if price_request.price_basis.mode == "raw" else "adjusted_research") == "raw_cash"
    declared = {request.kind: request for request in requirement.requests if request.kind != "bars"}
    if raw and (price_request.price_basis.mode != "raw" or not {"instruments", "trading_status", "corporate_actions"}.issubset(declared)):
        raise ValueError("raw_cash requires raw prices and factual instruments/status/actions")
    def table(kind):
        request = declared[kind]
        return market.read_table(snapshot, kind, TableQuery(**request.model_dump(exclude={"kind", "frequency", "price_basis", "currency"})))
    def read(request):
        frames = []
        for instrument in request.instruments:
            bases = {item["price_basis"]["basis_id"] for item in snapshot.quality.get("series", [])
                     if item["kind"] == "bars" and instrument.key in item["instruments"]
                     and item["price_basis"]["mode"] == request.price_basis.mode}
            if request.price_basis.mode == "vendor_research":
                if len(bases) != 1:
                    raise MarketDataError("SEMANTIC_CONFLICT", "snapshot must resolve one research basis per instrument")
                basis = request.price_basis.model_copy(update={"basis_id": bases.pop()})
            else:
                basis = request.price_basis
            query = DataQuery(**{**request.model_dump(), "instruments": (instrument,), "price_basis": basis})
            frame = market.read_bars(snapshot, query)
            frame["symbol"] = instrument.code
            frames.append(frame)
        result = pd.concat(frames, ignore_index=True).rename(columns={"trading_date": "date"})
        result["date"] = pd.to_datetime(result.date)
        return result
    prices = read(price_request)[["date", "symbol", "open", "high", "low", "close", "volume", "amount"]]
    benchmark_request = next(request for request in bars if len(request.instruments) == 1 and request.instruments[0].instrument_type == "index")
    benchmark = read(benchmark_request)[["date", "close", "amount"]].rename(columns={"amount": "turnover"})
    calendar = pd.DataFrame({"date": pd.to_datetime(table("calendar").trading_date).drop_duplicates().sort_values()}).reset_index(drop=True)
    if prices.empty or not (calendar.date > prices.date.max()).any():
        raise ValueError("calendar requires data and a following trading session")
    first, last = prices.date.min().date(), prices.date.max().date()
    symbol_map = {item.key: item.code for item in price_request.instruments}
    def symbols(frame):
        return frame.rename(columns={"instrument_id": "symbol", "trading_date": "date"}).assign(symbol=lambda value: value.symbol.map(symbol_map))
    if raw:
        instruments = symbols(table("instruments"))[INSTRUMENT_COLUMNS]
        status = symbols(table("trading_status"))[["date", "symbol", "status", "can_buy", "can_sell"]]
        events = symbols(table("corporate_actions"))[list(EVENT_COLUMNS)]
    else:
        instruments = pd.DataFrame(research_assumptions.get("instruments") or [{"symbol": item.code, "lot_size": 100,
            "settlement_sessions": 1, "listing_date": first, "group": "research"} for item in price_request.instruments])
        sessions = calendar.loc[calendar.date.between(pd.Timestamp(first), pd.Timestamp(last)), "date"]
        status = pd.DataFrame([dict(date=day, symbol=item.code, status="trading", can_buy=True, can_sell=True)
            for item in price_request.instruments for day in sessions])
        events = pd.DataFrame(columns=EVENT_COLUMNS)
    auxiliary = {kind.partition(":")[2]: table(kind) for kind in declared if kind.startswith("auxiliary:")}
    series = [item for item in snapshot.quality.get("series", []) if item["kind"] == "bars"]
    units = [item["units"] for item in series]
    volume_unit = "share" if units and all(unit.get("volume") == "share" for item, unit in zip(series, units) if set(item["instruments"]) & set(symbol_map)) else "unknown"
    amount_unit = "CNY" if units and all(unit.get("amount") == "CNY" for unit in units) else "unknown"
    if raw and (volume_unit != "share" or amount_unit != "CNY"):
        raise ValueError("raw_cash requires verified volume and amount units")
    manifest = DatasetManifest(source=f"market snapshot {snapshot.id}", is_demo=False, price_mode="raw_cash" if raw else "adjusted_research",
        currency=price_request.currency, calendar_start=calendar.date.min().date(), calendar_end=calendar.date.max().date(), data_start=first, data_end=last,
        volume_unit=volume_unit, amount_unit=amount_unit, event_coverage="complete" if raw else "unknown", benchmark_type="price_index" if raw else "other",
        field_semantics={"amount": "market source amount; see unit evidence", "turnover": "selected benchmark amount, not market-wide turnover"},
        universe_rule=research_assumptions.get("universe_rule", {"min_history_days": 1, "min_avg_turnover_20d": 0}),
        auxiliary_tables={name: f"aux/{name}.parquet" for name in auxiliary},
        limitations=list(snapshot.coverage.limitations) + ([] if raw else RESEARCH_LIMITATIONS))
    return validate_dataset(DatasetBundle(manifest, prices, benchmark, calendar, instruments, status, events, pd.DataFrame(), auxiliary))
