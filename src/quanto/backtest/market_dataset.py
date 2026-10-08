"""Daily backtest datasets assembled from prepared market snapshots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from quanto.backtest.dataset_loaders import validate_dataset
from quanto.backtest.dataset_schemas import DatasetBundle, DatasetManifest, INSTRUMENT_COLUMNS
from quanto.data import MarketData
from quanto.data.contracts import DataQuery, DataRequest, DataRequirement, MarketDataError, PriceBasis, QualityPolicy, SnapshotRef, TableQuery


EVENT_COLUMNS = ("symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio")
RESEARCH_LIMITATIONS = ["供应商价格口径仅用于研究，不模拟真实分红和拆分现金流。",
    "交易标志、每手数量和结算周期为研究模型假设，不是真实可交易状态证据。",
    "首次观测日期仅是研究覆盖边界，未核实真实上市日。"]


def build_data_requirement(payload: dict) -> DataRequirement:
    from quanto.workbench.framework_requests import FetchRequest
    request = FetchRequest.model_validate({key: value for key, value in payload.items() if key in FetchRequest.model_fields})
    start_day = request.historical_start or request.start_date
    end_day = request.end_date
    start = datetime.combine(start_day, datetime.min.time(), timezone.utc)
    end = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), timezone.utc)
    instruments = request.instruments
    mode = request.price_mode
    basis = PriceBasis(mode="raw" if mode == "raw_cash" else "vendor_research")
    requests = [DataRequest(kind="bars", instruments=instruments, start=start, end=end,
        columns=("open", "high", "low", "close", "volume", "amount"), frequency="1d", price_basis=basis),
        DataRequest(kind="bars", instruments=(request.benchmark,),
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
    price_request = next(request for request in bars if all(item.instrument_type != "index" for item in request.instruments))
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
            frame["symbol"] = instrument.key
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
    symbol_map = {item.key: item.key for item in price_request.instruments}
    def symbols(frame):
        if frame.instrument_id.isna().any() or not set(frame.instrument_id).issubset(symbol_map):
            raise ValueError("BINDING_MISMATCH: unknown source instrument identity")
        return frame.rename(columns={"instrument_id": "symbol", "trading_date": "date"}).assign(symbol=lambda value: value.symbol.map(symbol_map))
    if raw:
        instruments = symbols(table("instruments"))[INSTRUMENT_COLUMNS]
        status = symbols(table("trading_status"))[["date", "symbol", "status", "can_buy", "can_sell"]]
        events = symbols(table("corporate_actions"))[list(EVENT_COLUMNS)]
    else:
        if any(item.instrument_type != "etf" for item in price_request.instruments):
            raise ValueError("EXECUTION_CAPABILITY_MISSING: research trading assumptions are defined for ETF data only")
        instruments = pd.DataFrame(research_assumptions.get("instruments") or [{"symbol": item.key, "lot_size": 100,
            "settlement_sessions": 1, "listing_date": first, "group": "research"} for item in price_request.instruments])
        sessions = calendar.loc[calendar.date.between(pd.Timestamp(first), pd.Timestamp(last)), "date"]
        status = pd.DataFrame([dict(date=day, symbol=item.key, status="trading", can_buy=True, can_sell=True)
            for item in price_request.instruments for day in sessions])
        events = pd.DataFrame(columns=EVENT_COLUMNS)
    auxiliary = {kind.partition(":")[2]: table(kind) for kind in declared if kind.startswith("auxiliary:")}
    for name, frame in auxiliary.items():
        for column in ("symbol", "instrument_id"):
            if column in frame and (frame[column].isna().any() or not set(frame[column]).issubset(symbol_map)):
                raise ValueError(f"BINDING_MISMATCH: unknown canonical identity in auxiliary {name}.{column}")
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


def market_asset_binding(dataset_id, content_hash, bundle, market, snapshot, *, allow_local=False):
    """Prove identities against the registered snapshot, not a user supplied name."""
    from pandas.testing import assert_frame_equal
    from quanto.backtest.dataset_identity import CORE_TABLES, standard_table_semantics, validate_binding_tables
    from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding, TableSemanticBinding
    manifest = market.store.verify_snapshot(snapshot)
    bars = [item for item in manifest.requirement.requests if item.kind == "bars"]
    trade_requests = [item for item in bars if all(asset.instrument_type != "index" for asset in item.instruments)]
    references = [item for item in bars if len(item.instruments) == 1 and item.instruments[0].instrument_type == "index"]
    if len(trade_requests) != 1 or len(references) != 1:
        raise ValueError("IDENTITY_UNRESOLVED: snapshot does not establish unique trade and benchmark roles")
    identities = trade_requests[0].instruments
    actual = set(bundle.instruments.symbol)
    if actual == {item.key for item in identities}:
        mapping = {item.key: item.key for item in identities}
    elif allow_local and len({item.code for item in identities}) == len(identities) and actual == {item.code for item in identities}:
        # The old assembler's documented encoding, proven from explicit snapshot identities.
        mapping = {item.key: item.code for item in identities}
    else:
        raise ValueError("BINDING_MISMATCH: snapshot identities cannot be mapped unambiguously")
    expected = assemble_dataset(market, snapshot, manifest.requirement, {"price_mode": bundle.price_mode, "universe_rule": bundle.manifest.universe_rule})
    if bundle.manifest.source != f"market snapshot {snapshot.id}":
        raise ValueError("BINDING_MISMATCH: dataset source differs from registered snapshot")
    for field in ("currency", "volume_unit", "amount_unit", "benchmark_type", "event_coverage"):
        if getattr(bundle.manifest, field) != getattr(expected.manifest, field):
            raise ValueError(f"BINDING_MISMATCH: snapshot {field} differs")
    for name in (*CORE_TABLES, *expected.auxiliary):
        left = getattr(bundle, name) if name in CORE_TABLES else bundle.auxiliary.get(name)
        right = (getattr(expected, name) if name in CORE_TABLES else expected.auxiliary[name]).copy()
        for column in ("symbol", "instrument_id"):
            if column in right:
                right[column] = right[column].map(mapping)
        if left is None:
            raise ValueError(f"BINDING_MISMATCH: missing snapshot table {name}")
        if set(left.columns) != set(right.columns):
            raise ValueError(f"BINDING_MISMATCH: unverified columns in {name}")
        try:
            columns = list(right.columns)
            assert_frame_equal(left[columns].sort_values(columns).reset_index(drop=True),
                               right.sort_values(columns).reset_index(drop=True), check_dtype=False, check_exact=True)
        except (AssertionError, KeyError) as exc:
            raise ValueError(f"BINDING_MISMATCH: {name} differs from registered snapshot") from exc
    ref = f"market:{snapshot.id}"
    semantics = list(standard_table_semantics(bundle, ref))
    for name in bundle.auxiliary:
        fragments = [item for item in manifest.fragments if item.request.kind == f"auxiliary:{name}"]
        declarations = [item.source.parameters.get("table_semantics") for item in fragments]
        if not declarations or any(item is None or item != declarations[0] for item in declarations):
            raise ValueError(f"IDENTITY_UNRESOLVED: source has no unambiguous semantics for {name}")
        semantics.append(TableSemanticBinding(**{**declarations[0], "table": name, "evidence_ref": ref, "declaration_hash": ""}))
    assets = tuple(AssetMapping(instrument_id=item, local_symbol=mapping[item.key], evidence_ref=ref) for item in identities)
    validate_binding_tables(bundle, assets, tuple(semantics))
    return AssetBinding(dataset_version_id=dataset_id, dataset_content_hash=content_hash, assets=assets,
                        benchmark=BenchmarkBinding(instrument_id=references[0].instruments[0], return_basis=bundle.manifest.benchmark_type, evidence_ref=ref),
                        table_semantics=tuple(semantics))
