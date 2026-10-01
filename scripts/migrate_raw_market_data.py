"""Import the audited legacy raw inventory; verify before retiring any source file."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path

import pandas as pd

from quanto.data import MarketData, get_market_root
from quanto.data.contracts import BAR_COLUMNS, DataRequest, FragmentRef, InstrumentId, NormalizedChunk, PriceBasis, SourceRecord, content_key
from quanto.data.providers.base import daily_bars
from quanto.data.validation import validate_chunk


AUDITS = ("docs/research/2026-10-01/source_audit.json", "docs/research/2026-10-01-round2/fresh_source_audit.json", "docs/research/2026-10-01-round2/tencent_requests.json")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def inventory(source):
    audits = [json.loads((source / name).read_text(encoding="utf-8")) for name in AUDITS]
    entries = []
    for group, rows in (("old", audits[0]["inputs"]), ("tencent", audits[1]["source_files"])):
        for row in rows:
            path = row["path" if group == "old" else "raw_copy_path"].replace("\\", "/")
            if group == "old" and not path.startswith("data/raw/"):
                continue
            resolved = (source / path).resolve()
            if not resolved.is_relative_to((source / "data/raw").resolve()) or resolved.suffix != ".csv":
                raise ValueError("audit path escapes raw inventory")
            entries.append(dict(original_path=path, group=group,
                input_sha256=row["sha256" if group == "old" else "raw_copy_sha256"],
                rows=row["row_count" if group == "old" else "rows"],
                first_date=row["coverage_start" if group == "old" else "first_date"],
                last_date=row["coverage_end" if group == "old" else "last_date"]))
    expected = {entry["original_path"] for entry in entries}
    actual = {path.relative_to(source).as_posix() for path in (source / "data/raw").rglob("*.csv")}
    if not entries or len(expected) != len(entries) or expected != actual:
        raise ValueError("raw inventory differs from audited CSV set")
    return sorted(entries, key=lambda entry: entry["original_path"]), audits[2]


def csv_frame(data, entry):
    frame = pd.read_csv(BytesIO(data), dtype={"symbol": str})
    dates = pd.to_datetime(frame.date)
    if len(frame) != entry["rows"] or dates.min().date().isoformat() != entry["first_date"] or dates.max().date().isoformat() != entry["last_date"]:
        raise ValueError("CSV rows or date range differs from audit")
    return frame


def normalized(data, entry, request, source):
    frame = csv_frame(data, entry)
    instrument = request.instruments[0]
    if "symbol" in frame and not frame.symbol.eq(instrument.code).all():
        raise ValueError("CSV symbol differs from instrument; preserve leading zeroes")
    frame = frame.rename(columns={"turnover": "amount"}).drop(columns="symbol", errors="ignore")
    frame = daily_bars(frame, instrument, request)
    chunk = validate_chunk(NormalizedChunk(request, frame, source, data))
    if len(chunk.frame) != entry["rows"]:
        raise ValueError("normalization dropped source rows")
    return chunk


def metadata(data, entry, requests, imported_at, audit_hashes):
    frame = csv_frame(data, entry)
    path = Path(entry["original_path"])
    is_index = path.parent.name == "benchmark"
    code = path.stem.removeprefix("sh")
    instrument = InstrumentId(exchange="SSE" if is_index or code.startswith("5") else "SZSE", instrument_type="index" if is_index else "etf", code=code)
    downloads = [row for row in requests if row["symbol"] == code and row["status"] == 200 and row.get("provider_code") == 0] if entry["group"] == "tencent" else []
    if entry["group"] == "tencent" and not downloads:
        raise ValueError("missing Tencent download provenance")
    known_download = max((row["received_utc"] for row in downloads), default=None)
    parameters = dict(original_path=entry["original_path"], input_sha256=entry["input_sha256"],
        audit_sha256=audit_hashes, original_downloaded_at=known_download,
        timestamp_role="download" if known_download else "migration_only",
        requested_adjustment="qfq", adjustment_anchor="unknown",
        returned_keys=sorted({row["returned_key"] for row in downloads}),
        download_requests=downloads,
        provenance="Frozen CSV with historical audit; original old-provider responses unavailable")
    source = SourceRecord(provider="legacy-tencent" if downloads else "legacy-akshare", interface="audited-csv-migration",
        upstream="https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get" if downloads else "historical AKShare fetch; endpoint not recorded",
        parameters=parameters, downloaded_at=known_download or imported_at,
        actual_mode="day" if is_index and downloads else "qfq_requested_unverified",
        units={"volume": "unknown", "amount": "unknown"},
        limitations=("unverified_price_basis", "unknown_units", "unknown_availability"),
        availability_assumption="Historical quote availability was not recorded")
    basis = content_key([entry["group"], audit_hashes[AUDITS[1] if downloads else AUDITS[0]], "index" if is_index else "qfq"])
    columns = tuple(column for column in BAR_COLUMNS[4:] if column in frame or column == "amount" and "turnover" in frame)
    request = DataRequest(kind="bars", instruments=(instrument,), start=pd.Timestamp(entry["first_date"], tz="UTC").to_pydatetime(),
        end=(pd.Timestamp(entry["last_date"], tz="UTC") + pd.Timedelta(days=1)).to_pydatetime(),
        columns=columns, frequency="1d", price_basis=PriceBasis(mode="vendor_research", basis_id=basis))
    return request, source


def verify_entries(market, entries, *, registered, check_activity=False):
    for entry in entries:
        fragments = tuple(FragmentRef.model_validate(ref) for ref in entry["fragments"])
        if len(fragments) != 1 or fragments[0].source_evidence_hash != entry["input_sha256"]:
            raise ValueError("migration receipt evidence differs")
        fragment = fragments[0]
        data = market.read_evidence(entry["input_sha256"])
        expected = normalized(data, entry, fragment.request, fragment.source).frame
        market.store.verify_fragment(fragment)
        actual = pd.read_parquet(market.store.object_path(fragment))[list(expected.columns)]
        pd.testing.assert_frame_equal(expected, actual, check_exact=True)
        if registered:
            with market.catalog.connection() as db:
                row = db.execute("SELECT payload, active FROM fragments WHERE id=?", (content_key(fragment.model_dump(mode="json")),)).fetchone()
            if not row or FragmentRef.model_validate_json(row[0]) != fragment or check_activity and bool(row[1]) != entry["active"]:
                raise ValueError("migration catalog registration differs from receipt")


def verify_report(root, report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if report["validation"] != "passed" or len(report["sources"]) != report["source_count"] or sum(entry["rows"] for entry in report["sources"]) != report["source_rows"]:
        raise ValueError("invalid migration receipt")
    with MarketData(root, providers=()) as market:
        verify_entries(market, report["sources"], registered=True)
    return report


def migrate(root, source_root, report_path):
    source, report_path = Path(source_root).resolve(), Path(report_path)
    entries, downloads = inventory(source)
    audit_hashes = {name: sha256((source / name).read_bytes()) for name in AUDITS}
    previous = verify_report(root, report_path) if report_path.exists() else None
    if previous and previous["audit_sha256"] != audit_hashes:
        raise ValueError("audit changed since migration")
    if previous:
        if any(sha256((source / entry["original_path"]).read_bytes()) != entry["input_sha256"] for entry in entries):
            raise ValueError("CSV hash differs from audit")
        return previous
    imported_at = datetime.now(timezone.utc).isoformat()
    fresh_codes = {Path(entry["original_path"]).stem for entry in entries if entry["group"] == "tencent"}
    with MarketData(root, providers=()) as market:
        all_fragments, inactive = [], []
        for entry in entries:
            data = (source / entry["original_path"]).read_bytes()
            if sha256(data) != entry["input_sha256"]:
                raise ValueError(f"CSV hash differs from audit: {entry['original_path']}")
            request, record = metadata(data, entry, downloads, imported_at, audit_hashes)
            fragments = market.store.put_chunk(normalized(data, entry, request, record))
            active = entry["group"] == "tencent" or Path(entry["original_path"]).stem not in fresh_codes
            entry.update(active=active, fragments=[ref.model_dump(mode="json") for ref in fragments])
            all_fragments.extend(fragments)
            if not active:
                inactive.extend(fragments)
        verify_entries(market, entries, registered=False)
        # Check every input again before the atomic catalog registration.
        if any(sha256((source / entry["original_path"]).read_bytes()) != entry["input_sha256"] for entry in entries):
            raise ValueError("input changed during migration")
        market.catalog.register_fragments(tuple(all_fragments), inactive=tuple(inactive))
        verify_entries(market, entries, registered=True, check_activity=True)
    report = dict(validation="passed", imported_at=imported_at, market_root=str(Path(root).resolve()), audit_sha256=audit_hashes,
        source_count=len(entries), active_source_count=sum(entry["active"] for entry in entries), source_rows=sum(entry["rows"] for entry in entries),
        policy="Tencent frozen sequence active where available; overlapping old basis retained as evidence, never spliced",
        readiness="Imported bars only; no official calendar or ready snapshot synthesized", sources=entries)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if not args.verify and args.source_root is None:
        parser.error("--source-root is required for migration")
    report = verify_report(get_market_root(), args.report) if args.verify else migrate(get_market_root(), args.source_root, args.report)
    print(json.dumps({key: report[key] for key in ("validation", "market_root", "source_count", "active_source_count", "source_rows")}))


if __name__ == "__main__":
    main()
