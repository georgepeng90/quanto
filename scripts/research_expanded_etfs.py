"""Cache and compare a frozen expanded ETF universe without retuning strategies.

Run with ``python scripts/research_expanded_etfs.py --phase cache|assemble|backtest
--selection selection.json --output <new-directory>``. The configured shared
market library is used; an explicit --root must name that same library.
Selection is a list, or a mapping with a ``symbols`` list, of objects containing
symbol, name, category and an evidenced listing_date. Ranking metadata is retained
in the input file; selection is retrospective and is not a point-in-time universe.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import pandas as pd

from quanto.backtest.dataset_loaders import load_dataset, validate_dataset
from quanto.backtest.dataset_storage import write_parquet_dataset
from quanto.backtest.market_dataset import assemble_dataset, build_data_requirement
from quanto.data import MarketConfig, MarketData
from quanto.data.contracts import DataRequirement, MarketDataError, QualityPolicy, content_key
from quanto.data.settings import get_market_root
from quanto.data.providers.akshare import AKShareProvider
from quanto.data.providers.tencent import TencentProvider


PROJECT = Path(__file__).resolve().parents[1]
HISTORY_START = "2022-10-01"
END = "2026-09-30"
PERIODS = {"full": ["2023-10-09", END], "middle": ["2023-10-09", "2024-12-31"],
           "recent": ["2025-01-02", END], "post_selection": ["2026-05-20", END]}
FROZEN = PROJECT / "outputs/strategy-research-20261001-round2/fresh/protocol.json"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix(path.suffix + ".tmp")
    staged.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    staged.replace(path)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_unit_evidence(path):
    path = Path(path).resolve()
    audit = read_json(path)
    if (audit.get("date") != END or audit.get("volume") != "lot100"
            or audit.get("amount") != "ten_thousand_CNY"):
        raise ValueError("unit audit date or conversion convention differs")
    sina_path = path.parent / "sina_etfs.csv"
    if digest(sina_path) != audit.get("sina_source_sha256"):
        raise ValueError("unit audit Sina evidence changed")
    quotes = pd.read_csv(sina_path, encoding="utf-8-sig", dtype={"代码": str})
    quotes = {row["代码"][2:]: row for _, row in quotes.iterrows()}
    seen = set()
    for row in audit.get("rows", []):
        symbol = row["symbol"]
        if symbol in seen:
            raise ValueError("duplicate symbol in unit audit")
        seen.add(symbol)
        if row.get("passed") is not True:
            continue
        raw_path = path.parent / "unit_evidence" / f"{symbol}.json"
        if digest(raw_path) != row.get("raw_sha256"):
            raise ValueError(f"unit audit Tencent evidence changed: {symbol}")
        ticker = ("sh" if symbol.startswith(("5", "6")) else "sz") + symbol
        raw = read_json(raw_path)
        if raw.get("code") != 0:
            raise ValueError(f"unit audit Tencent response failed: {symbol}")
        daily = raw["data"][ticker]["day"][-1]
        quote = quotes[symbol]
        volume, amount = float(daily[5]) * 100, float(daily[8]) * 10000
        implied = amount / volume if volume else 0
        expected = {"date": daily[0], "tencent_volume": float(daily[5]), "tencent_amount": float(daily[8]),
            "sina_volume": float(quote["成交量"]), "sina_amount": float(quote["成交额"]),
            "raw_low": float(daily[4]), "raw_high": float(daily[3]), "implied_price": implied}
        if (any(row.get(key) != value for key, value in expected.items())
                or daily[0] != END or not all(math.isfinite(value) for key, value in expected.items() if key != "date")
                or volume <= 0 or amount <= 0 or abs(volume - expected["sina_volume"]) > 100
                or abs(amount - expected["sina_amount"]) > 110
                or not expected["raw_low"] - .005 <= implied <= expected["raw_high"] + .005):
            raise ValueError(f"unit audit numerical proof failed: {symbol}")
    return audit | {"artifact": str(path), "artifact_sha256": digest(path)}


def validate_evidence(evidence):
    if not evidence or load_unit_evidence(evidence["artifact"]) != evidence:
        raise ValueError("unit audit artifact is missing or changed")


def load_selection(path):
    payload = read_json(path)
    rows = payload["symbols"] if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("selection requires a nonempty list of ETF objects")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not {"symbol", "name", "category", "listing_date"}.issubset(row):
            raise ValueError("selection requires symbol, name, category, listing_date")
        symbol = row["symbol"]
        if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isdigit() or symbol in seen:
            raise ValueError("invalid or duplicate ETF symbol")
        if not row["name"] or not row["category"] or date.fromisoformat(row["listing_date"]) > date.fromisoformat(END):
            raise ValueError("invalid ETF metadata or listing date after study end")
        seen.add(symbol)
    return rows


def requirement_for(row):
    requirement = build_data_requirement({"symbols": [row["symbol"]],
        "start_date": HISTORY_START, "end_date": END})
    requests = list(requirement.requests)
    requests[0] = requests[0].model_copy(update={"start": pd.Timestamp(max(HISTORY_START, row["listing_date"]), tz="UTC").to_pydatetime()})
    return requirement.model_copy(update={"requests": tuple(requests)})


class AuditedTencent:
    """ETF spot checks establish no index-unit convention."""
    name = "tencent"

    def __init__(self, audit=None):
        self.audit = audit or {}
        self.passed = {row["symbol"] for row in self.audit.get("rows", []) if row.get("passed") is True}
        self.unknown = TencentProvider()

    def capabilities(self):
        return self.unknown.capabilities()

    def fetch(self, request, context):
        instrument = request.instruments[0]
        if instrument.instrument_type == "etf" and instrument.code in self.passed:
            evidence = {"evidence": {"artifact": self.audit["artifact"], "sha256": self.audit["artifact_sha256"],
                                     "symbol": instrument.code, "scope": "recent independent spot checks"},
                        "volume": "lot100", "amount": "ten_thousand_CNY"}
            return TencentProvider(unit_evidence=evidence).fetch(request, context)
        return self.unknown.fetch(request, context)


def open_market(root, evidence=None):
    config = MarketConfig(source_priority=("tencent", "akshare"), max_workers=1,
        max_retries=1, request_timeout_seconds=60)
    return MarketData(root, config, providers=(AuditedTencent(evidence), AKShareProvider()))


def validate_progress(rows, root, progress):
    expected = {"market_root": str(Path(root).resolve()),
                "history_start": HISTORY_START, "end": END}
    original = progress.get("selection", rows)
    lookup = {row["symbol"]: row for row in original}
    if (progress.get("selection_key") != content_key(original)
            or any(lookup.get(row["symbol"]) != row for row in rows)
            or any(progress.get(key) != value for key, value in expected.items())):
        raise ValueError("cache identity changed; choose a new output directory")


def cache(rows, root, output, evidence=None):
    validate_evidence(evidence)
    output = Path(output)
    path = output / "cache_progress.json"
    if path.exists():
        progress = read_json(path)
        validate_progress(rows, root, progress)
        if progress.get("unit_evidence_key") != content_key(evidence):
            raise ValueError("unit evidence changed; choose a new output directory")
    else:
        progress = {"selection_key": content_key(rows), "selection": rows, "market_root": str(Path(root).resolve()),
                    "history_start": HISTORY_START, "end": END, "unit_evidence_key": content_key(evidence),
                    "unit_evidence": evidence, "symbols": {}}
        write_json(path, progress)
    with open_market(root, evidence) as market:
        for row in rows:
            requirement = requirement_for(row)
            previous = progress["symbols"].get(row["symbol"])
            if previous and previous["state"] == "ready":
                validate_snapshot(market, row, previous, evidence)
                continue
            if row["symbol"] not in AuditedTencent(evidence).passed:
                progress["symbols"][row["symbol"]] = {"state": "incomplete", "reason": "ETF unit audit did not pass"}
                write_json(path, progress)
                continue
            # Refresh may expand to old active intervals. Its calendar must cover
            # that union even though the study's ETF listing boundary stays fixed.
            etf, _, calendar = requirement.requests
            old = market.catalog.fragments(DataRequirement(requests=(etf,)))
            calendar = calendar.model_copy(update={"start": min([calendar.start, *(ref.request.start for ref in old)]),
                "end": max([calendar.end, *(ref.request.end + pd.Timedelta(days=14) for ref in old)])})
            refresh_requirement = DataRequirement(requests=(etf, calendar), quality=QualityPolicy(required_evidence=("units",),
                accepted_limitations=("unverified_price_basis", "unknown_availability")))
            task = market.prefetch(refresh_requirement, refresh=True)
            try:
                market.wait_ready(task.id)
                task = market.prefetch(requirement)
                market.wait_ready(task.id)
            except MarketDataError:
                pass
            result = market.get_prefetch(task.id).model_dump(mode="json")
            progress["symbols"][row["symbol"]] = {**result, "requirement_key": requirement.key,
                "requirement": requirement.model_dump(mode="json"), "listing_date": row["listing_date"],
                "fresh_audited_refresh": True, "unit_evidence_key": content_key(evidence)}
            if result["state"] == "ready":
                validate_snapshot(market, row, progress["symbols"][row["symbol"]], evidence)
            write_json(path, progress)
            ready = sum(item["state"] == "ready" for item in progress["symbols"].values())
            print(json.dumps({"symbol": row["symbol"], "state": result["state"], "ready": ready,
                              "selected": len(rows), "reason": result.get("reason")}, ensure_ascii=False), flush=True)
            time.sleep(1)
    return progress


def validate_snapshot(market, row, entry, evidence):
    requirement = requirement_for(row)
    if (entry.get("state") != "ready" or entry.get("fresh_audited_refresh") is not True
            or entry.get("unit_evidence_key") != content_key(evidence)):
        raise ValueError("ETF snapshot lacks a fresh audited refresh")
    ref = market.resolve_snapshot(entry["snapshot"]["id"])
    if (entry.get("snapshot") != ref.model_dump(mode="json") or entry["requirement_key"] != requirement.key
            or ref.quality["requirement_key"] != requirement.key or not ref.coverage.complete):
        raise ValueError("cached snapshot requirement or integrity changed")
    manifest = market.store.verify_snapshot(ref)
    fragments = [fragment for fragment in manifest.fragments if requirement.requests[0].instruments[0] in fragment.request.instruments]
    profile = AuditedTencent(evidence)
    if row["symbol"] not in profile.passed or not fragments:
        raise ValueError("ETF snapshot has no passed unit audit")
    expected_evidence = {"artifact": evidence["artifact"], "sha256": evidence["artifact_sha256"],
        "symbol": row["symbol"], "scope": "recent independent spot checks"}
    bases = set()
    for fragment in fragments:
        basis = fragment.request.price_basis
        source = fragment.source
        unit_profile = source.parameters.get("unit_evidence", {})
        if (source.provider != "tencent" or source.actual_mode not in {"day", "qfqday"}
                or source.units != {"volume": "share", "amount": "CNY"} or "units" not in source.verified
                or unit_profile.get("evidence") != expected_evidence
                or basis.mode != "vendor_research" or not basis.basis_id):
            raise ValueError("ETF snapshot units, source, mode or audit evidence do not match")
        bases.add((basis.basis_id, source.actual_mode))
    if len(bases) != 1:
        raise ValueError("ETF snapshot has inconsistent price basis or mode")
    return ref


def assemble_symbol(market, row, entry, evidence):
    requirement = requirement_for(row)
    ref = validate_snapshot(market, row, entry, evidence)
    requests = list(requirement.requests)
    requests[1] = requests[1].model_copy(update={"start": requests[0].start})
    local_requirement = requirement.model_copy(update={"requests": tuple(requests)})
    assumptions = {"instruments": [{"symbol": row["symbol"], "lot_size": 100,
        "settlement_sessions": 1, "listing_date": row["listing_date"], "group": row["category"]}],
        "universe_rule": {"min_history_days": 120, "min_avg_turnover_20d": 0}}
    bundle = assemble_dataset(market, ref, local_requirement, assumptions)
    # Dataset amount_unit describes ETF turnover, while the shared index feed
    # retains unknown units in the snapshot evidence and disclosed limitations.
    etf_key = requests[0].instruments[0].key
    series = [item for item in ref.quality["series"] if item["kind"] == "bars" and etf_key in item["instruments"]]
    amount_unit = "CNY" if series and all(item["units"].get("amount") == "CNY" for item in series) else "unknown"
    manifest = bundle.manifest.model_copy(update={"amount_unit": amount_unit,
        "limitations": bundle.manifest.limitations + ["Benchmark feed amount units are unknown; ETF unit conversion is supported only by recent per-symbol spot checks; historical unit conventions are assumed.",
            "Tencent price modes are consistent within each full refreshed ETF window; adjustment anchors remain unverified."]})
    return replace(bundle, manifest=manifest)


def qualify(rows, root, output, minimum_amount=10_000_000):
    output = Path(output)
    progress = read_json(output / "cache_progress.json")
    validate_progress(rows, root, progress)
    evidence = progress.get("unit_evidence")
    validate_evidence(evidence)
    selected, decisions = [], []
    with MarketData(root, providers=()) as market:
        for row in rows:
            entry = progress["symbols"].get(row["symbol"], {})
            decision = {"symbol": row["symbol"], "qualified": False, "cache_state": entry.get("state", "missing")}
            if entry.get("state") != "ready":
                decision["reason"] = "cache not ready"
            else:
                bundle = assemble_symbol(market, row, entry, evidence)
                recent = bundle.prices.sort_values("date").tail(60)
                if bundle.manifest.amount_unit != "CNY":
                    decision["reason"] = "ETF amount unit not evidenced as CNY"
                elif len(recent) < 60:
                    decision["reason"] = "fewer than 60 observations"
                else:
                    decision["average_amount_60d_CNY"] = float(recent.amount.mean())
                    decision["qualified"] = decision["average_amount_60d_CNY"] >= minimum_amount
                    decision["reason"] = "passed" if decision["qualified"] else "below liquidity threshold"
                if decision["qualified"]:
                    selected.append(row)
            decisions.append(decision)
    result = {"symbols": selected, "metadata": {"minimum_average_amount_60d_CNY": minimum_amount,
        "source_selection_key": progress["selection_key"], "candidate_count": len(rows), "qualified_count": len(selected),
        "rule": "Current trailing liquidity screen; not a point-in-time historical eligibility rule."}, "decisions": decisions}
    path = output / "qualified_selection.json"
    if path.exists() and read_json(path) != result:
        raise ValueError("qualification changed; preserve the prior file and choose a new output")
    write_json(path, result)
    print(json.dumps(result["metadata"], ensure_ascii=False), flush=True)
    return result


def require_ready(rows, output, minimum=200):
    if len(rows) < minimum:
        raise ValueError(f"at least {minimum} complete ready ETFs required; selected {len(rows)}")
    progress = read_json(Path(output) / "cache_progress.json")
    missing = [row["symbol"] for row in rows if progress["symbols"].get(row["symbol"], {}).get("state") != "ready"]
    if missing:
        raise ValueError(f"selected ETFs not ready ({len(missing)}): {', '.join(missing)}")
    return progress


def combine_bundles(bundles):
    first = bundles[0]
    def combine(name):
        return pd.concat([getattr(bundle, name) for bundle in bundles], ignore_index=True).drop_duplicates().reset_index(drop=True)
    prices, benchmark, calendar = (combine(name) for name in ("prices", "benchmark", "calendar"))
    limitations = sorted({item for bundle in bundles for item in bundle.manifest.limitations})
    limitations += ["Expanded universe chosen using current rankings/news has survivorship and retrospective selection bias.",
                    "Official listing dates come from the selected catalog; trading flags, lot size and T+1 remain research assumptions."]
    manifest = first.manifest.model_copy(update={"source": "Expanded ETF research; per-symbol immutable market snapshots",
        "data_start": prices.date.min().date(), "data_end": prices.date.max().date(),
        "calendar_start": calendar.date.min().date(), "calendar_end": calendar.date.max().date(),
        "volume_unit": first.manifest.volume_unit if all(b.manifest.volume_unit == first.manifest.volume_unit for b in bundles) else "unknown",
        "amount_unit": first.manifest.amount_unit if all(b.manifest.amount_unit == first.manifest.amount_unit for b in bundles) else "unknown",
        "limitations": limitations, "universe_rule": {"min_history_days": 120, "min_avg_turnover_20d": 0}})
    return validate_dataset(replace(first, manifest=manifest, prices=prices, benchmark=benchmark, calendar=calendar,
        instruments=combine("instruments"), status=combine("status"), events=combine("events")))


def assemble(rows, root, output):
    output = Path(output)
    progress = require_ready(rows, output, minimum=1)
    validate_progress(rows, root, progress)
    evidence = progress.get("unit_evidence")
    validate_evidence(evidence)
    audit_path, dataset = output / "source_audit.json", output / "dataset"
    if audit_path.exists():
        audit = read_json(audit_path)
        if audit["selection_key"] != content_key(rows):
            raise ValueError("assembled selection changed; choose a new output directory")
        if audit["progress_key"] != content_key(progress):
            raise ValueError("cache changed after assembly")
        for name, expected in audit["data_files"].items():
            if digest(dataset / name) != expected:
                raise ValueError("assembled dataset changed")
        return load_dataset(dataset)
    if dataset.exists():
        raise FileExistsError("unpublished dataset directory exists; inspect before resuming")
    bundles = []
    with MarketData(root, providers=()) as market:
        for row in rows:
            entry = progress["symbols"][row["symbol"]]
            bundles.append(assemble_symbol(market, row, entry, evidence))
    bundle = combine_bundles(bundles)
    if set(bundle.prices.symbol) != {row["symbol"] for row in rows}:
        raise ValueError("assembled pool differs from selected pool")
    write_parquet_dataset(bundle, dataset)
    load_dataset(dataset)
    write_json(audit_path, {"validation": "passed", "progress_key": content_key(progress),
        "selection_key": content_key(rows),
        "selected_count": len(rows), "price_rows": len(bundle.prices), "limitations": bundle.manifest.limitations,
        "snapshots": {row["symbol"]: progress["symbols"][row["symbol"]]["snapshot"] for row in rows},
        "data_files": {p.name: digest(p) for p in sorted(dataset.iterdir()) if p.is_file()}})
    return bundle


def validate_qualification(rows, progress, output):
    qualification_path = output / "qualified_selection.json"
    if not qualification_path.exists():
        raise ValueError("qualified_selection.json is required before backtest")
    qualification = read_json(qualification_path)
    if qualification.get("symbols") != rows:
        raise ValueError("backtest selected pool differs from qualified_selection.json")
    metadata = qualification.get("metadata", {})
    threshold = metadata.get("minimum_average_amount_60d_CNY")
    if (type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold < 10_000_000):
        raise ValueError("backtest liquidity threshold must be finite and at least CNY 10,000,000")
    if metadata.get("source_selection_key") != progress["selection_key"]:
        raise ValueError("qualification source selection differs from cache progress")
    decisions = qualification.get("decisions", [])
    selected_decisions = {row["symbol"]: [decision for decision in decisions if decision.get("symbol") == row["symbol"]]
                          for row in rows}
    for symbol, matches in selected_decisions.items():
        if len(matches) != 1 or matches[0].get("qualified") is not True:
            raise ValueError(f"selected ETF lacks one qualifying liquidity decision: {symbol}")
        amount = matches[0].get("average_amount_60d_CNY")
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < threshold:
            raise ValueError(f"selected ETF fails the frozen liquidity threshold: {symbol}")
    return qualification_path


def backtest(rows, root, output, pool="expanded"):
    output = Path(output).resolve()
    if pool not in {"expanded", "original27"}:
        raise ValueError("unsupported research pool")
    original = read_json(FROZEN)
    if pool == "original27" and (len(rows) != 27 or {row["symbol"] for row in rows} != set(original["spec"]["symbols"])):
        raise ValueError("original27 control must contain exactly the frozen 27 ETF symbols")
    progress = require_ready(rows, output, minimum=200 if pool == "expanded" else 27)
    qualification_path = validate_qualification(rows, progress, output) if pool == "expanded" else None
    bundle = assemble(rows, root, output)
    # Import existing frozen implementations, leaving every original artifact intact.
    sys.path.insert(0, str(PROJECT / "scripts"))
    import research_round2 as study
    spec = copy.deepcopy(original["spec"])
    spec["symbols"] = sorted(row["symbol"] for row in rows)
    added_groups = sorted(set(bundle.instruments.group) - set(spec["risk"]["group_caps"]))
    spec["risk"]["group_caps"].update({group: .5 for group in added_groups})
    sources = [*sorted((PROJECT / "src/quanto").rglob("*.py")), Path(__file__),
               PROJECT / "scripts/research_round2.py", PROJECT / "scripts/research_round2_candidates.py",
               PROJECT / "scripts/research_rotation_candidates.py", PROJECT / "uv.lock"]
    protocol = {"purpose": "Matched-period ETF universe comparison, same eight configurations; no retuning", "pool": pool,
        "periods": PERIODS, "candidates": original["candidates"], "spec": spec,
        "dataset": str(output / "dataset"), "audit": str(output / "source_audit.json"),
        "audit_sha256": digest(output / "source_audit.json"), "original_protocol_sha256": digest(FROZEN),
        "qualified_selection_sha256": digest(qualification_path) if qualification_path else None,
        "data_files": {p.name: digest(p) for p in sorted((output / "dataset").iterdir()) if p.is_file()},
        "source_files": {p.relative_to(PROJECT).as_posix(): digest(p) for p in sources},
        "added_groups": added_groups, "group_cap_mapping": "Each new catalog category inherits the same original 0.5 group cap.",
        "limitations": bundle.manifest.limitations + ["The post_selection interval was already inspected in earlier research and is not a new holdout.",
            "Catalog categories differ from the original groups; the same numerical group caps can impose different concentration constraints.",
            "BuyHold selects only once; ETFs that become eligible later are not added automatically."]}
    path = output / "protocol.json"
    if path.exists():
        if read_json(path) != protocol:
            raise ValueError("frozen expanded protocol changed")
    else:
        write_json(path, protocol)
    study.verify(protocol)
    results = []
    for period in PERIODS:
        for identity in protocol["candidates"]:
            row = study.execute(str(output), identity, period, 1.)
            results.append(row)
            write_json(output / "results.json", results)
            pd.DataFrame([{ "candidate": result["candidate"], "period": result["period"], **result["metrics"]}
                for result in results]).to_csv(output / "results.csv", index=False, encoding="utf-8-sig")
            print(json.dumps({"candidate": identity, "period": period, "total_return": row["metrics"]["total_return"],
                              "max_drawdown": row["metrics"]["max_drawdown"]}, ensure_ascii=False), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("cache", "qualify", "assemble", "backtest"))
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--unit-evidence", type=Path)
    parser.add_argument("--min-average-amount", type=float, default=10_000_000)
    parser.add_argument("--pool", choices=("expanded", "original27"), default="expanded")
    args = parser.parse_args()
    configured_root = get_market_root().resolve()
    if args.root is not None and args.root.resolve() != configured_root:
        parser.error("--root must match the configured shared market root")
    args.root = configured_root
    rows = load_selection(args.selection)
    if args.phase == "cache":
        if args.unit_evidence is None:
            parser.error("--unit-evidence is required for cache")
        evidence = load_unit_evidence(args.unit_evidence)
        progress = cache(rows, args.root, args.output, evidence)
        return 0 if all(progress["symbols"].get(row["symbol"], {}).get("state") == "ready" for row in rows) else 2
    if args.phase == "qualify":
        qualify(rows, args.root, args.output, args.min_average_amount)
    elif args.phase == "assemble":
        assemble(rows, args.root, args.output)
    else:
        backtest(rows, args.root, args.output, args.pool)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
