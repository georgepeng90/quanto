"""Freeze the authorized eight-ETF daily research dataset; never train a model."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
import time
from datetime import datetime, timezone
import urllib.request

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from scripts import research_expanded_etfs as pipeline
from quanto.data.providers.tencent import transport
from quanto.data.settings import get_market_root
from quanto.backtest.dataset_loaders import validate_dataset
from quanto.data.contracts import DataRequest, DataRequirement, SourceRecord, QualityPolicy, content_key, NormalizedChunk
from quanto.data.providers.base import ProviderCapabilities
from quanto.data.coverage import fragment_frame
from quanto.data.imports import import_table
import quanto.backtest.market_dataset as market_dataset

OUTPUT = PROJECT / "outputs/rl-four-sector-20261005/data-prep"
PREVIOUS = PROJECT / "outputs/etf-expansion-20261002"
ROWS = [
    ("516080", "易方达中证创新药产业ETF", "healthcare", "2021-02-23"),
    ("159898", "招商中证全指医疗器械ETF", "healthcare", "2021-06-04"),
    ("512760", "国泰CES半导体芯片行业ETF", "technology", "2019-06-12"),
    ("515880", "国泰中证全指通信设备ETF", "technology", "2019-09-06"),
    ("516090", "易方达中证新能源ETF", "energy", "2021-03-19"),
    ("159806", "国泰中证新能源汽车ETF", "energy", "2020-03-20"),
    ("515020", "华夏中证银行ETF", "financials", "2019-12-03"),
    ("515010", "华夏中证全指证券公司ETF", "financials", "2019-11-07"),
]
SUSPENSIONS = {
    "512760": ("2020-09-09", "https://static.cninfo.com.cn/finalpage/2020-09-03/1208381264.PDF"),
    "516090": ("2021-11-26", "https://pdf.dfcfw.com/pdf/H2_AN202111291531775489_1.pdf"),
}
original_requirement = pipeline.requirement_for


def validate_snapshot(market, row, entry, evidence):
    req = requirement_for(row)
    ref = market.resolve_snapshot(entry["snapshot"]["id"])
    if (entry["state"] != "ready" or not entry.get("fresh_audited_refresh")
            or entry.get("unit_evidence_key") != content_key(evidence)
            or entry["snapshot"] != ref.model_dump(mode="json")
            or entry["requirement_key"] != req.key or ref.quality["requirement_key"] != req.key
            or not ref.coverage.complete):
        raise ValueError("snapshot identity or audited refresh does not match")
    manifest = market.store.verify_snapshot(ref)
    fragments = [f for f in manifest.fragments if f.request.kind == "bars"
        and req.requests[0].instruments[0] in f.request.instruments]
    expected = {"artifact": evidence["artifact"], "sha256": evidence["artifact_sha256"],
        "symbol": row["symbol"], "scope": "recent independent spot checks"}
    bases = set()
    if not fragments or row["symbol"] not in pipeline.AuditedTencent(evidence).passed:
        raise ValueError("no audited ETF bars")
    for f in fragments:
        if (f.source.provider != "tencent" or f.source.actual_mode not in {"day", "qfqday"}
                or f.source.units != {"volume": "share", "amount": "CNY"}
                or "units" not in f.source.verified
                or f.source.parameters.get("unit_evidence", {}).get("evidence") != expected
                or f.request.price_basis.mode != "vendor_research" or not f.request.price_basis.basis_id):
            raise ValueError("ETF units or price basis not audited")
        bases.add((f.request.price_basis.basis_id, f.source.actual_mode))
    if len(bases) != 1:
        raise ValueError("mixed price bases")
    return ref


def suspension_request(row):
    day, _ = SUSPENSIONS[row["symbol"]]
    instrument = original_requirement(row).requests[0].instruments[0]
    return DataRequest(kind="trading_status", instruments=(instrument,),
        start=pd.Timestamp(day, tz="UTC").to_pydatetime(),
        end=(pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=1)).to_pydatetime(),
        columns=("instrument_id", "trading_date", "status"))


def requirement_for(row):
    req = original_requirement(row)
    if row["symbol"] == "516090":
        req = req.model_copy(update={"requests": (*req.requests, suspension_request(row))})
    return req


def prepare_suspensions(selection, root, evidence):
    with pipeline.open_market(root, evidence) as market:
        for row in selection:
            if row["symbol"] not in SUSPENSIONS:
                continue
            day, url = SUSPENSIONS[row["symbol"]]
            path = OUTPUT / f"suspension-{row['symbol']}.pdf"
            if not path.exists():
                with urllib.request.urlopen(url, timeout=60) as response:
                    path.write_bytes(response.read())
            raw = path.read_bytes()
            if not raw.startswith(b"%PDF"):
                raise ValueError("official suspension evidence is not PDF")
            req = suspension_request(row)
            frame = pd.DataFrame([dict(instrument_id=req.instruments[0].key,
                trading_date=day, status="suspended")])
            source = SourceRecord(provider="official_announcement", interface="fund_split_suspension",
                upstream=url, parameters={"announcement": str(path), "sha256": pipeline.digest(path),
                    "scope": "only the explicitly documented suspension date"},
                downloaded_at=datetime.now(timezone.utc), actual_mode="documented_suspension",
                verified=("status",))
            import_table(market.store, market.catalog, req, frame, source, raw)


def repair_cache(selection, root, evidence):
    path = OUTPUT / "cache_progress.json"
    progress = pipeline.read_json(path)
    with pipeline.open_market(root, evidence) as market:
        class OfficialSuspensionProvider:
            name = "official_announcement"

            def capabilities(self):
                return ProviderCapabilities(kinds=("trading_status",), frequencies=(), fields={})

            def fetch(self, request, context):
                refs = market.catalog.fragments(DataRequirement(requests=(request,)))
                ref = next(ref for ref in refs if ref.source.provider == self.name and ref.request == request)
                raw = (OUTPUT / f"suspension-{request.instruments[0].code}.pdf").read_bytes()
                return NormalizedChunk(request, fragment_frame(market.store, ref), ref.source, raw)

        market._providers += (OfficialSuspensionProvider(),)
        for row in selection:
            if row["symbol"] not in SUSPENSIONS:
                continue
            requirement = requirement_for(row)
            prior = progress["symbols"].get(row["symbol"])
            if prior and prior["state"] == "ready" and prior["requirement_key"] == requirement.key:
                validate_snapshot(market, row, prior, evidence)
                continue
            etf, _, calendar = requirement.requests[:3]
            old = market.catalog.fragments(DataRequirement(requests=(etf,)))
            calendar = calendar.model_copy(update={"start": min([calendar.start, *(ref.request.start for ref in old)]),
                "end": max([calendar.end, *(ref.request.end + pd.Timedelta(days=14) for ref in old)])})
            refresh = DataRequirement(requests=(etf, calendar, suspension_request(row)),
                quality=QualityPolicy(required_evidence=("units",),
                    accepted_limitations=("unverified_price_basis", "unknown_availability")))
            task = market.prefetch(refresh, refresh=True)
            market.wait_ready(task.id)
            task = market.prefetch(requirement)
            market.wait_ready(task.id)
            result = market.get_prefetch(task.id).model_dump(mode="json")
            progress["symbols"][row["symbol"]] = {**result, "requirement_key": requirement.key,
                "requirement": requirement.model_dump(mode="json"), "listing_date": row["listing_date"],
                "fresh_audited_refresh": True, "unit_evidence_key": content_key(evidence)}
            pipeline.validate_snapshot(market, row, progress["symbols"][row["symbol"]], evidence)
            pipeline.write_json(path, progress)
            print(json.dumps({"symbol": row["symbol"], "state": result["state"], "suspension_resolved": True}), flush=True)


def validate_with_suspensions(bundle):
    for symbol, (day, _) in SUSPENSIONS.items():
        keep = (bundle.status.symbol == symbol) & (bundle.status.date == pd.Timestamp(day))
        bundle.status.loc[keep, ["status", "can_buy", "can_sell"]] = ["suspended", False, False]
    return validate_dataset(bundle)


def unit_audit(selection):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / "unit_audit.json"
    if target.exists():
        return pipeline.load_unit_evidence(target)
    shutil.copy2(PREVIOUS / "sina_etfs.csv", OUTPUT / "sina_etfs.csv")
    quotes = pd.read_csv(OUTPUT / "sina_etfs.csv", encoding="utf-8-sig", dtype={"代码": str})
    quotes = {row["代码"][2:]: row for _, row in quotes.iterrows()}
    prior = pipeline.load_unit_evidence(PREVIOUS / "unit_audit.json")
    known = {row["symbol"]: row for row in prior["rows"] if row.get("passed")}
    evidence_dir = OUTPUT / "unit_evidence"
    evidence_dir.mkdir(exist_ok=True)
    results = []
    for item in selection:
        symbol = item["symbol"]
        if symbol in known:
            shutil.copy2(PREVIOUS / "unit_evidence" / f"{symbol}.json", evidence_dir / f"{symbol}.json")
            results.append(known[symbol])
            print(json.dumps({"symbol": symbol, "unit_evidence": "reused_verified"}), flush=True)
            continue
        ticker = ("sh" if symbol.startswith("5") else "sz") + symbol
        error = None
        for attempt in range(3):
            try:
                raw = transport({"param": f"{ticker},day,2026-09-25,2026-09-30,10,"}, 30)
                parsed = json.loads(raw.decode().split("=", 1)[-1].strip().rstrip(";"))
                daily = parsed["data"][ticker]["day"][-1]
                quote = quotes[symbol]
                volume, amount = float(daily[5]) * 100, float(daily[8]) * 10000
                implied = amount / volume if volume else 0
                passed = (parsed.get("code") == 0 and daily[0] == pipeline.END
                    and all(math.isfinite(v) for v in (volume, amount, implied))
                    and volume > 0 and amount > 0
                    and abs(volume - float(quote["成交量"])) <= 100
                    and abs(amount - float(quote["成交额"])) <= 110
                    and float(daily[4]) - .005 <= implied <= float(daily[3]) + .005)
                (evidence_dir / f"{symbol}.json").write_bytes(raw)
                results.append(dict(symbol=symbol, passed=passed, date=daily[0],
                    tencent_volume=float(daily[5]), tencent_amount=float(daily[8]),
                    sina_volume=float(quote["成交量"]), sina_amount=float(quote["成交额"]),
                    raw_low=float(daily[4]), raw_high=float(daily[3]), implied_price=implied,
                    raw_sha256=hashlib.sha256(raw).hexdigest()))
                error = None
                break
            except Exception as exc:
                error = repr(exc)
                time.sleep(2)
        if error:
            results.append(dict(symbol=symbol, passed=False, error=error))
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    pipeline.write_json(target, dict(purpose="Frozen Sina 2026-09-30 snapshot and Tencent raw day cross-source unit spot checks",
        date=pipeline.END, volume="lot100", amount="ten_thousand_CNY",
        sina_source_sha256=pipeline.digest(OUTPUT / "sina_etfs.csv"), rows=results))
    if not all(row.get("passed") for row in results):
        raise ValueError("unit audit failed; see preserved evidence")
    return pipeline.load_unit_evidence(target)


def audit_bundle(bundle):
    validate_dataset(bundle)
    prices = bundle.prices.sort_values(["symbol", "date"])
    common_start = pd.Timestamp("2021-06-04")
    end = pd.Timestamp(pipeline.END)
    calendar = set(bundle.calendar.loc[bundle.calendar.date.between(common_start, end), "date"])
    decision = pd.Timestamp("2022-01-07")
    checks = []
    for symbol, frame in prices.groupby("symbol"):
        dates = set(frame.loc[frame.date.between(common_start, end), "date"])
        invalid = ((frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
            | (frame.high < frame[["open", "close", "low"]].max(axis=1))
            | (frame.low > frame[["open", "close", "high"]].min(axis=1))
            | (frame[["volume", "amount"]] < 0).any(axis=1)
            | frame[["open", "high", "low", "close", "volume", "amount"]].isna().any(axis=1))
        pre = int((frame.date < decision).sum())
        checks.append(dict(symbol=symbol, rows=len(frame), first=str(frame.date.min().date()),
            last=str(frame.date.max().date()), duplicate_dates=int(frame.date.duplicated().sum()),
            common_missing_dates=[str(v.date()) for v in sorted(calendar - dates)],
            unexplained_missing_dates=[str(v.date()) for v in sorted(calendar - dates)
                if symbol not in SUSPENSIONS or str(v.date()) != SUSPENSIONS[symbol][0]],
            absolute_daily_returns_over_20_percent=[dict(date=str(day.date()), daily_return=float(value))
                for day, value in zip(frame.date, frame.close.pct_change()) if abs(value) > .2],
            invalid_ohlc_rows=int(invalid.sum()), warmup_bars_before_2022_first_decision=pre,
            warmup_120_passed=pre >= 120,
            average_amount_last_60_sessions_CNY=float(frame.tail(60).amount.mean())))
    close = prices.pivot(index="date", columns="symbol", values="close")
    returns = close.loc[close.index >= common_start].pct_change(fill_method=None)
    pair = returns[["516090", "159806"]].dropna()
    result = dict(dataset=str((OUTPUT / "dataset").resolve()), history_start=pipeline.HISTORY_START,
        end=pipeline.END, common_start=str(common_start.date()), common_calendar_sessions=len(calendar),
        first_decision=str(decision.date()), checks=checks,
        energy_daily_return_correlation=float(pair.corr().iloc[0, 1]), energy_correlation_observations=len(pair),
        event_rows=len(bundle.events), price_mode=bundle.price_mode,
        limitations=list(bundle.manifest.limitations))
    result["documented_suspensions"] = {s: dict(date=d, official_url=u) for s, (d, u) in SUSPENSIONS.items()}
    result["passed"] = all(not x["unexplained_missing_dates"] and not x["invalid_ohlc_rows"]
        and not x["duplicate_dates"] and x["warmup_120_passed"] for x in checks)
    pipeline.write_json(OUTPUT / "training_data_audit.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not result["passed"]:
        raise ValueError("dataset quality audit failed")


def disclose_evidence(bundle):
    """Enrich metadata once; preserve every frozen market price unchanged."""
    manifest_path = OUTPUT / "dataset/manifest.json"
    audit_path = OUTPUT / "source_audit.json"
    audit = pipeline.read_json(audit_path)
    if audit.get("official_suspension_evidence"):
        return bundle
    suspensions = {symbol: dict(date=day, official_url=url,
        downloaded_pdf=str(OUTPUT / f"suspension-{symbol}.pdf"),
        sha256=pipeline.digest(OUTPUT / f"suspension-{symbol}.pdf"))
        for symbol, (day, url) in SUSPENSIONS.items()}
    note = ("Official fund announcements document 512760 suspension on 2020-09-09 (before study) "
        "and 516090 suspension on 2021-11-26; the latter is represented as suspended with no quote "
        "and no buying/selling. Other daily trading flags remain research assumptions. "
        "Healthcare Tencent day mode is supported by third-party pages reporting no distributions/splits; "
        "complete official corporate-action coverage remains unverified.")
    bundle.manifest.limitations.append(note)
    bundle.manifest.field_semantics["documented_suspensions"] = json.dumps(suspensions)
    pipeline.write_json(manifest_path, bundle.manifest.model_dump(mode="json"))
    audit["official_suspension_evidence"] = suspensions
    audit["healthcare_day_mode_check"] = {
        "516080": {"source": "https://fundf10.eastmoney.com/fhsp_516080.html", "page_date": "2026-09-23",
            "observed": "No distribution or split records listed"},
        "159898": {"source": "https://fundf10.eastmoney.com/fhsp_159898.html", "page_date": "2026-06-16",
            "observed": "No distribution or split records listed"},
        "limitation": "Third-party absence is supporting evidence, not complete official corporate-action verification."}
    audit["limitations"] = bundle.manifest.limitations
    audit["data_files"]["manifest.json"] = pipeline.digest(manifest_path)
    pipeline.write_json(audit_path, audit)
    return pipeline.load_dataset(OUTPUT / "dataset")


def main():
    pipeline.HISTORY_START = "2021-01-01"
    selection = [dict(zip(("symbol", "name", "category", "listing_date"), row)) for row in ROWS]
    pipeline.write_json(OUTPUT / "selection.json", {"symbols": selection})
    evidence = unit_audit(selection)
    root = get_market_root()
    frozen = (OUTPUT / "source_audit.json").exists()
    if not frozen:
        prepare_suspensions(selection, root, evidence)
        if not (OUTPUT / "cache_progress.json").exists():
            pipeline.cache(selection, root, OUTPUT, evidence)
    pipeline.requirement_for = requirement_for
    pipeline.validate_snapshot = validate_snapshot
    if not frozen:
        repair_cache(selection, root, evidence)
    market_dataset.validate_dataset = validate_with_suspensions
    bundle = pipeline.assemble(selection, get_market_root(), OUTPUT)
    bundle = disclose_evidence(bundle)
    audit_bundle(bundle)


if __name__ == "__main__":
    main()
