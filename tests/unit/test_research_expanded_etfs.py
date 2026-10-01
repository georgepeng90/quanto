"""Audited shared snapshots and frozen matched-period research gates."""
from contextlib import nullcontext
from types import SimpleNamespace
import json
import sys

import pandas as pd
import pytest

from scripts import research_expanded_etfs as study
from quanto.data import MarketConfig, MarketData
from quanto.data.contracts import content_key
from quanto.data.providers.akshare import AKShareProvider
from quanto.data.providers.tencent import TencentProvider


def selection(*codes):
    return [{"symbol": code, "name": code, "category": "technology", "listing_date": "2024-01-01"} for code in codes]


def evidence_for(tmp_path, rows):
    directory = tmp_path / "proof"
    (directory / "unit_evidence").mkdir(parents=True, exist_ok=True)
    quotes, audited = [], []
    for row in rows:
        symbol = row["symbol"]
        ticker = ("sh" if symbol.startswith("5") else "sz") + symbol
        daily = [study.END, "1", "1", "1.1", ".9", "1000", {}, "0", "10"]
        path = directory / "unit_evidence" / f"{symbol}.json"
        study.write_json(path, {"code": 0, "data": {ticker: {"day": [daily]}}})
        quotes.append({"代码": ticker, "成交量": 100000, "成交额": 100000})
        audited.append({"symbol": symbol, "passed": True, "date": study.END,
            "tencent_volume": 1000., "tencent_amount": 10., "sina_volume": 100000., "sina_amount": 100000.,
            "raw_low": .9, "raw_high": 1.1, "implied_price": 1., "raw_sha256": study.digest(path)})
    pd.DataFrame(quotes).to_csv(directory / "sina_etfs.csv", index=False, encoding="utf-8-sig")
    path = directory / "unit_audit.json"
    study.write_json(path, {"date": study.END, "volume": "lot100", "amount": "ten_thousand_CNY",
        "sina_source_sha256": study.digest(directory / "sina_etfs.csv"), "rows": audited})
    return study.load_unit_evidence(path)


@pytest.fixture
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(study, "HISTORY_START", "2024-01-01")
    monkeypatch.setattr(study, "END", "2024-01-05")
    monkeypatch.setattr(study.time, "sleep", lambda _: None)
    calls = []
    def transport(params, timeout):
        calls.append(params)
        ticker = params["param"].split(",")[0]
        raw = [[day.date().isoformat(), "1", "1", "1.1", ".9", "1000", {}, "0", "10"]
            for day in pd.bdate_range("2023-01-02", "2024-01-05")]
        return json.dumps({"code": 0, "data": {ticker: {"qfqday": raw}}}).encode()
    def sdk(function, kwargs, context):
        assert function == "tool_trade_date_hist_sina", "unknown-unit fallback must not qualify"
        return pd.DataFrame({"trade_date": pd.bdate_range("2023-01-02", "2024-01-31")})
    monkeypatch.setattr(study, "TencentProvider", lambda unit_evidence=None: TencentProvider(transport, unit_evidence))
    def market(root, evidence=None):
        return MarketData(root, MarketConfig(source_priority=("tencent", "akshare"), max_retries=0, max_workers=1),
            (study.AuditedTencent(evidence), AKShareProvider(sdk_call=sdk)))
    monkeypatch.setattr(study, "open_market", market)
    return tmp_path / "shared", tmp_path / "research", calls


def test_listing_boundary_keeps_shared_benchmark_history():
    etf, benchmark, calendar = study.requirement_for(selection("512480")[0] | {"listing_date": "2025-03-04"}).requests
    assert etf.start.date().isoformat() == "2025-03-04"
    assert benchmark.start.date().isoformat() == "2022-10-01"
    assert etf.end.date().isoformat() == "2026-10-01"
    assert calendar.end > etf.end


def test_cli_uses_configured_shared_root_and_rejects_output_market(tmp_path, monkeypatch):
    path = tmp_path / "selection.json"
    study.write_json(path, selection("512480"))
    root, output = tmp_path / "shared", tmp_path / "output"
    monkeypatch.setattr(study, "get_market_root", lambda: root)
    seen = []
    monkeypatch.setattr(study, "qualify", lambda rows, root, output, threshold: seen.append(root))
    args = ["research", "--phase", "qualify", "--selection", str(path), "--output", str(output)]
    monkeypatch.setattr(sys, "argv", args)
    assert study.main() == 0
    assert seen == [root.resolve()]
    monkeypatch.setattr(sys, "argv", args + ["--root", str(output / "market")])
    with pytest.raises(SystemExit) as failure:
        study.main()
    assert failure.value.code == 2
    assert not (output / "market").exists()


@pytest.mark.parametrize("change", ["raw", "sina", "numbers"])
def test_unit_audit_checks_bytes_and_numerical_proof(tmp_path, change):
    evidence = evidence_for(tmp_path, selection("512480"))
    path = study.Path(evidence["artifact"])
    if change == "raw":
        (path.parent / "unit_evidence" / "512480.json").write_bytes(b"changed")
    elif change == "sina":
        (path.parent / "sina_etfs.csv").write_text("changed", encoding="utf-8")
    else:
        payload = study.read_json(path)
        payload["rows"][0]["sina_amount"] = 1
        study.write_json(path, payload)
    with pytest.raises(ValueError, match="evidence changed|numerical proof"):
        study.load_unit_evidence(path)


def test_missing_or_failed_audit_never_fetches_or_becomes_ready(tmp_path, offline):
    root, output, calls = offline
    rows = selection("512480")
    with pytest.raises(ValueError, match="missing"):
        study.cache(rows, root, output)
    evidence = evidence_for(tmp_path, rows)
    path = study.Path(evidence["artifact"])
    payload = study.read_json(path)
    payload["rows"][0]["passed"] = False
    study.write_json(path, payload)
    result = study.cache(rows, root, output, study.load_unit_evidence(path))
    assert result["symbols"]["512480"]["state"] == "incomplete"
    assert calls == []


def test_ready_resume_no_fetch_and_assembly_includes_all_symbols(tmp_path, offline, monkeypatch):
    root, output, calls = offline
    rows = selection("512480", "159995")
    rows[1]["listing_date"] = "2024-01-03"
    evidence = evidence_for(tmp_path, rows)
    first = study.cache(rows, root, output, evidence)
    assert all(entry["state"] == "ready" for entry in first["symbols"].values())
    count = len(calls)
    monkeypatch.setattr(study, "open_market", lambda root, evidence=None: MarketData(root, providers=()))
    assert study.cache(rows, root, output, evidence) == first
    assert len(calls) == count
    bundle = study.assemble(rows, root, output)
    assert set(bundle.prices.symbol) == {"512480", "159995"}
    assert len(bundle.prices) == 8
    assert bundle.manifest.amount_unit == "CNY"
    assert bundle.manifest.volume_unit == "share"
    assert "unknown_units" in bundle.manifest.limitations
    assert "unverified_price_basis" in bundle.manifest.limitations


def test_refresh_replaces_unknown_history_keeps_old_evidence_and_study_boundary(tmp_path, offline):
    root, output, calls = offline
    rows = selection("512480")
    requirement = study.requirement_for(rows[0])
    old_requirement = requirement.model_copy(update={"requests": tuple(
        request.model_copy(update={"start": pd.Timestamp("2023-01-02", tz="UTC").to_pydatetime()})
        for request in requirement.requests)})
    with study.open_market(root) as market:
        old = market.wait_ready(market.prefetch(old_requirement).id)
        manifest = market.store.verify_snapshot(old)
        old_evidence = {fragment.source_evidence_hash: market.read_evidence(fragment.source_evidence_hash)
            for fragment in manifest.fragments}
    result = study.cache(rows, root, output, evidence_for(tmp_path, rows))
    entry = result["symbols"]["512480"]
    assert entry["state"] == "ready"
    assert entry["snapshot"]["id"] != old.id
    with MarketData(root, providers=()) as market:
        assert market.resolve_snapshot(old.id) == old
        assert all(market.read_evidence(identity) == raw for identity, raw in old_evidence.items())
        manifest = market.store.verify_snapshot(market.resolve_snapshot(entry["snapshot"]["id"]))
        etf = next(fragment for fragment in manifest.fragments if fragment.request.instruments
            and fragment.request.instruments[0].instrument_type == "etf")
        assert etf.request.start.year == 2023
        assert etf.source.units == {"volume": "share", "amount": "CNY"}
    bundle = study.assemble(rows, root, output)
    assert bundle.prices.date.min().date().isoformat() == "2024-01-01"
    assert len(bundle.prices) == 5


@pytest.mark.parametrize("change", ["refresh", "snapshot", "audit"])
def test_ready_label_cannot_bypass_audit_snapshot_gates(tmp_path, offline, change):
    root, output, _ = offline
    rows = selection("512480")
    evidence = evidence_for(tmp_path, rows)
    result = study.cache(rows, root, output, evidence)
    if change == "refresh":
        result["symbols"]["512480"].pop("fresh_audited_refresh")
        study.write_json(output / "cache_progress.json", result)
    elif change == "snapshot":
        result["symbols"]["512480"]["snapshot"]["quality"]["requirement_key"] = "forged"
        study.write_json(output / "cache_progress.json", result)
    else:
        study.Path(evidence["artifact"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="refresh|integrity|audit"):
        study.assemble(rows, root, output)


def test_qualified_subset_identity_and_assembly_selection_are_frozen(tmp_path, offline):
    root, output, _ = offline
    rows = selection("512480", "159995")
    result = study.cache(rows, root, output, evidence_for(tmp_path, rows))
    study.validate_progress(rows[:1], root, result)
    with pytest.raises(ValueError, match="identity"):
        study.validate_progress([rows[0] | {"category": "changed"}], root, result)
    study.assemble(rows[:1], root, output)
    with pytest.raises(ValueError, match="selection"):
        study.assemble(rows, root, output)


def test_qualification_uses_sixty_day_mean_and_requires_cny(tmp_path, monkeypatch):
    rows = selection("512480", "159995", "515050")
    evidence = evidence_for(tmp_path, rows)
    progress = {"selection": rows, "selection_key": content_key(rows), "unit_evidence": evidence,
        "market_root": str(tmp_path.resolve()), "history_start": study.HISTORY_START, "end": study.END,
        "symbols": {row["symbol"]: {"state": "ready"} for row in rows}}
    study.write_json(tmp_path / "cache_progress.json", progress)
    bundles = {code: SimpleNamespace(manifest=SimpleNamespace(amount_unit=unit), prices=pd.DataFrame({
        "date": pd.bdate_range("2026-07-01", periods=60), "amount": amounts})) for code, unit, amounts in [
            ("512480", "CNY", [12_000_000] * 60), ("159995", "CNY", [1_000_000] * 59 + [60_000_000]),
            ("515050", "unknown", [100_000_000] * 60)]}
    monkeypatch.setattr(study, "MarketData", lambda *args, **kwargs: nullcontext(None))
    monkeypatch.setattr(study, "assemble_symbol", lambda market, row, entry, evidence: bundles[row["symbol"]])
    result = study.qualify(rows, tmp_path, tmp_path)
    assert [row["symbol"] for row in result["symbols"]] == ["512480"]
    assert result["decisions"][1]["reason"] == "below liquidity threshold"
    assert result["decisions"][2]["reason"] == "ETF amount unit not evidenced as CNY"


def test_expanded_gate_rejects_small_or_incomplete_pool_and_liquidity(tmp_path):
    rows = selection("512480")
    study.write_json(tmp_path / "cache_progress.json", {"symbols": {"512480": {"state": "ready"}}})
    with pytest.raises(ValueError, match="200"):
        study.backtest(rows, tmp_path, tmp_path)
    large = selection(*(f"{500000 + i:06d}" for i in range(200)))
    with pytest.raises(ValueError, match="not ready"):
        study.backtest(large, tmp_path, tmp_path)
    progress = {"selection_key": content_key(rows)}
    with pytest.raises(ValueError, match="qualified_selection"):
        study.validate_qualification(rows, progress, tmp_path)
    qualified = {"symbols": rows, "metadata": {"source_selection_key": progress["selection_key"],
        "minimum_average_amount_60d_CNY": 10_000_000}, "decisions": [
            {"symbol": "512480", "qualified": True, "average_amount_60d_CNY": 12_000_000}]}
    path = tmp_path / "qualified_selection.json"
    study.write_json(path, qualified)
    assert study.validate_qualification(rows, progress, tmp_path) == path
    for modified, message in [(qualified | {"symbols": []}, "selected pool"),
        (qualified | {"metadata": qualified["metadata"] | {"minimum_average_amount_60d_CNY": 1}}, "threshold"),
        (qualified | {"decisions": []}, "qualifying"),
        (qualified | {"decisions": qualified["decisions"] * 2}, "qualifying"),
        (qualified | {"decisions": [qualified["decisions"][0] | {"average_amount_60d_CNY": 1}]}, "liquidity")]:
        study.write_json(path, modified)
        with pytest.raises(ValueError, match=message):
            study.validate_qualification(rows, progress, tmp_path)


def test_original27_control_keeps_eight_configs_periods_and_costs(tmp_path, monkeypatch):
    original = study.read_json(study.FROZEN)
    rows = selection(*original["spec"]["symbols"])
    study.write_json(tmp_path / "cache_progress.json", {"symbols": {row["symbol"]: {"state": "ready"} for row in rows}})
    (tmp_path / "dataset").mkdir()
    study.write_json(tmp_path / "source_audit.json", {"validated": True})
    monkeypatch.setattr(study, "assemble", lambda *args: SimpleNamespace(
        instruments=pd.DataFrame({"group": ["technology"]}), manifest=SimpleNamespace(limitations=[])))
    monkeypatch.setattr(study, "validate_qualification", lambda *args: pytest.fail("control has no expanded liquidity gate"))
    sys.path.insert(0, str(study.PROJECT / "scripts"))
    import research_round2
    monkeypatch.setattr(research_round2, "verify", lambda protocol: None)
    calls = []
    def execute(root, identity, period, multiplier):
        calls.append((identity, period, multiplier))
        return {"candidate": identity, "period": period, "metrics": {"total_return": 0, "max_drawdown": 0}}
    monkeypatch.setattr(research_round2, "execute", execute)
    assert len(study.backtest(rows, tmp_path / "shared", tmp_path, "original27")) == 32
    protocol = study.read_json(tmp_path / "protocol.json")
    assert protocol["candidates"] == original["candidates"]
    assert protocol["spec"]["execution"] == original["spec"]["execution"]
    assert protocol["periods"] == study.PERIODS
    assert set(calls) == {(identity, period, 1.) for identity in original["candidates"] for period in study.PERIODS}
    with pytest.raises(ValueError, match="exactly"):
        study.backtest(rows[:-1], tmp_path / "shared", tmp_path, "original27")
