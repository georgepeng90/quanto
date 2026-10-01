import asyncio
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from quanto.data import MarketData, MarketDataError
from quanto.data.contracts import DataRequirement, FragmentRef, content_key
from quanto.data.settings import get_market_root
from scripts.migrate_raw_market_data import migrate, verify_report


def legacy_files(root):
    old, fresh = [], []
    for relative, close in (("etf/159915.csv", 10), ("etf/510300.csv", 11), ("benchmark/sh000300.csv", 3000),
                            ("tencent-20261001/etf/159915.csv", 12), ("tencent-20261001/benchmark/sh000300.csv", 3100)):
        path = root / "data/raw" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        code = path.stem.removeprefix("sh")
        frame = pd.DataFrame(dict(date=["2024-01-02", "2024-01-03"], symbol=code, open=close, high=close, low=close, close=close, volume=100, amount=1000))
        if relative == "benchmark/sh000300.csv":
            frame = frame[["date", "close", "amount"]].rename(columns={"amount": "turnover"})
        frame.to_csv(path, index=False)
        identity = hashlib.sha256(path.read_bytes()).hexdigest()
        relative_path = path.relative_to(root).as_posix()
        if relative.startswith("tencent"):
            fresh.append(dict(raw_copy_path=relative_path, raw_copy_sha256=identity, rows=2, first_date="2024-01-02", last_date="2024-01-03"))
        else:
            old.append(dict(path=relative_path, sha256=identity, row_count=2, coverage_start="2024-01-02", coverage_end="2024-01-03"))
    for folder, filename, data in (("2026-10-01", "source_audit.json", {"inputs": old}),
                                   ("2026-10-01-round2", "fresh_source_audit.json", {"source_files": fresh}),
                                   ("2026-10-01-round2", "tencent_requests.json", [dict(symbol=code, status=200, provider_code=0, received_utc="2024-01-04T00:00:00Z", returned_key="day" if code == "000300" else "qfqday") for code in ("159915", "000300")])):
        path = root / "docs/research" / folder / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def test_migration_preserves_all_values_and_evidence_but_selects_one_basis(tmp_path):
    source = tmp_path / "legacy"
    legacy_files(source)
    report_path = tmp_path / "report.json"
    report = migrate(get_market_root(), source, report_path)
    assert report["validation"] == "passed"
    assert report["source_count"] == 5
    assert report["active_source_count"] == 3
    assert report["source_rows"] == 10
    with MarketData(providers=()) as market:
        for entry in report["sources"]:
            ref = FragmentRef.model_validate(entry["fragments"][0])
            original = (source / entry["original_path"]).read_bytes()
            assert market.read_evidence(entry["input_sha256"]) == original
            assert asyncio.run(market.read_evidence_async(entry["input_sha256"])) == original
            frame = pd.read_parquet(market.store.object_path(ref))
            assert frame.instrument_id.eq(ref.request.instruments[0].key).all()
            assert ref.source.verified == ()
            assert ref.request.price_basis.mode == "vendor_research"
            assert not market.coverage(DataRequirement(requests=(ref.request,))).complete
            if entry["original_path"].endswith("benchmark/sh000300.csv"):
                assert ref.request.instruments[0].code == "000300"
        requests = tuple(FragmentRef.model_validate(entry["fragments"][0]).request.model_copy(update={"price_basis": FragmentRef.model_validate(entry["fragments"][0]).request.price_basis.model_copy(update={"basis_id": None})}) for entry in report["sources"])
        active = market.catalog.fragments(DataRequirement(requests=requests))
        assert len(active) == 3
        assert {ref.source.provider for ref in active} == {"legacy-akshare", "legacy-tencent"}
    assert verify_report(get_market_root(), report_path)["source_rows"] == 10


def test_repeat_migration_is_idempotent_and_report_survives_raw_removal(tmp_path):
    source = tmp_path / "legacy"
    legacy_files(source)
    report_path = tmp_path / "report.json"
    first = migrate(get_market_root(), source, report_path)
    assert migrate(get_market_root(), source, report_path) == first
    for path in (source / "data/raw").rglob("*.csv"):
        path.unlink()
    assert verify_report(get_market_root(), report_path)["validation"] == "passed"


def test_frozen_migration_proof_survives_later_series_refresh(tmp_path):
    source = tmp_path / "legacy"
    legacy_files(source)
    report_path = tmp_path / "report.json"
    report = migrate(get_market_root(), source, report_path)
    entry = next(entry for entry in report["sources"] if entry["active"])
    ref = FragmentRef.model_validate(entry["fragments"][0])
    with MarketData(providers=()) as market, market.catalog.connection() as db:
        db.execute("UPDATE fragments SET active=0 WHERE id=?", (content_key(ref.model_dump(mode="json")),))
    assert verify_report(get_market_root(), report_path) == report
    assert migrate(get_market_root(), source, report_path) == report
    with MarketData(providers=()) as market, market.catalog.connection() as db:
        assert db.execute("SELECT active FROM fragments WHERE id=?", (content_key(ref.model_dump(mode="json")),)).fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["hash", "symbol", "extra"])
def test_failed_migration_does_not_publish_catalog_or_success_report(tmp_path, damage):
    source = tmp_path / "legacy"
    legacy_files(source)
    path = source / "data/raw/etf/159915.csv"
    if damage == "hash":
        path.write_bytes(path.read_bytes() + b"\n")
    elif damage == "symbol":
        path.write_text(path.read_text().replace("159915", "159916"))
        audit = source / "docs/research/2026-10-01/source_audit.json"
        data = json.loads(audit.read_text())
        next(item for item in data["inputs"] if item["path"].endswith("159915.csv"))["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        audit.write_text(json.dumps(data))
    else:
        (path.parent / "extra.csv").write_bytes(path.read_bytes())
    report = tmp_path / "report.json"
    with pytest.raises(ValueError):
        migrate(get_market_root(), source, report)
    assert not report.exists()
    with MarketData(providers=()) as market, market.catalog.connection() as db:
        assert db.execute("SELECT count(*) FROM fragments").fetchone()[0] == 0


def test_evidence_tampering_and_invalid_id_are_rejected(tmp_path):
    source = tmp_path / "legacy"
    legacy_files(source)
    report_path = tmp_path / "report.json"
    report = migrate(get_market_root(), source, report_path)
    identity = report["sources"][0]["input_sha256"]
    (get_market_root() / "evidence" / identity[:2] / f"{identity}.bin").write_bytes(b"tampered")
    with MarketData(providers=()) as market:
        for value in (identity, "../outside", "x" * 64):
            with pytest.raises(MarketDataError, match="INTEGRITY_ERROR"):
                market.read_evidence(value)
    with pytest.raises(MarketDataError):
        verify_report(get_market_root(), report_path)
