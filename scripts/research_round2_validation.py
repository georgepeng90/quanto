"""Cost stress and a newly collected period for the already frozen shortlist."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
from datetime import datetime, timezone
import json
from pathlib import Path

import research_round2 as study


ROOT = study.DEFAULT_ROOT
DOCS = study.PROJECT / "docs/research/2026-10-01-round2"


def historical_inputs() -> tuple[dict, dict]:
    protocol = json.loads((ROOT / "protocol.json").read_text(encoding="utf-8"))
    selection = json.loads((DOCS / "selection.json").read_text(encoding="utf-8"))
    if selection["protocol_sha256"] != study.digest(ROOT / "protocol.json"):
        raise ValueError("Frozen shortlist/protocol mismatch")
    # Another task added strategy_name to API metadata after grid execution.
    # That module is not imported by this offline numerical runner. Preserve
    # the exact reviewed exception instead of weakening the original guard.
    evidence = json.loads((DOCS / "verification.json").read_text(encoding="utf-8"))
    allowed = {row["path"]: row for row in evidence["unrelated_source_drift"]}
    import sys
    for name, expected in protocol["source_files"].items():
        actual = study.digest(study.PROJECT / name)
        if actual != expected:
            exception = allowed.get(name, {})
            if (name != "src/quanto/workbench/service.py" or "quanto.workbench.service" in sys.modules
                    or exception.get("frozen_sha256") != expected or exception.get("current_sha256") != actual):
                raise ValueError(f"Unreviewed source change: {name}")
    for name, expected in protocol["data_files"].items():
        if study.digest(study.PROJECT / protocol["dataset"] / name) != expected:
            raise ValueError(f"Original dataset changed: {name}")
    if study.digest(study.PROJECT / protocol["audit"]) != protocol["audit_sha256"]:
        raise ValueError("Original audit changed")
    return protocol, selection


def initialize_fresh() -> dict:
    original, selection = historical_inputs()
    root = ROOT / "fresh"
    path = root / "protocol.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    dataset = root / "dataset"
    audit = root / "source_audit.json"
    names = selection["selected"] + study.BASELINES
    protocol = copy.deepcopy(original)
    protocol.update({
        "purpose": "Previously frozen shortlist; new data, 2026-05-20 through 2026-09-30; no retuning",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "periods": {"post_selection": ["2026-05-20", "2026-09-30"]},
        "candidates": {name: original["candidates"][name] for name in names},
        "dataset": dataset.relative_to(study.PROJECT).as_posix(),
        "audit": audit.relative_to(study.PROJECT).as_posix(),
        "audit_sha256": study.digest(audit),
        "data_files": {p.name: study.digest(p) for p in sorted(dataset.iterdir()) if p.is_file()},
        "historical_protocol_sha256": study.digest(ROOT / "protocol.json"),
        "selection_sha256": study.digest(DOCS / "selection.json"),
        "selection_commit": "2ea2b4f",
        "limitations": ["Only about four months; report cumulative returns prominently.",
                        "A new cash-start portfolio with warmup, not a continuation of the old-period account.",
                        "Provider forward-adjusted prices have a new anchor and are not spliced with old cached prices.",
                        "The selection reused historical data; new-period results do not remove universe/survivorship bias."],
    })
    protocol["source_files"] = {name: study.digest(study.PROJECT / name) for name in original["source_files"]}
    for path_to_source in (Path(__file__).resolve(), study.PROJECT / "scripts/prepare_round2_market_data.py"):
        protocol["source_files"][path_to_source.relative_to(study.PROJECT).as_posix()] = study.digest(path_to_source)
    study.write_json(path, protocol)
    return protocol


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["stress", "initialize-fresh", "fresh"], required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.phase == "stress":
        protocol, selection = historical_inputs()
        root = ROOT
        names = selection["selected"] + ["trend_20_20_k3", "buy_hold"]
        jobs = [(name, period, 2.) for period in ("recent", "full") for name in names]
    else:
        protocol = initialize_fresh()
        study.verify(protocol)
        if protocol["selection_sha256"] != study.digest(DOCS / "selection.json"):
            raise ValueError("Shortlist changed after new-data protocol freeze")
        if args.phase == "initialize-fresh":
            print(f"Frozen {len(protocol['candidates'])} new-period runs")
            return
        root = ROOT / "fresh"
        jobs = [(name, "post_selection", 1.) for name in protocol["candidates"]]
    outcomes = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(study.execute, str(root), *job) for job in jobs]
        for future in as_completed(futures):
            row = future.result()
            outcomes.append(row)
            print(json.dumps({"candidate": row["candidate"], "period": row["period"],
                              **{name: row["metrics"][name] for name in
                              ("total_return", "annual_return", "max_drawdown", "sharpe", "average_invested_ratio")}}, ensure_ascii=True), flush=True)
    if {(r["candidate"], r["period"], r["cost_multiplier"]) for r in outcomes} != set(jobs):
        raise ValueError("Incomplete validation run set")
    study.write_json(DOCS / f"{args.phase}_evidence.json", sorted(outcomes, key=lambda r: (r["period"], r["candidate"])))


if __name__ == "__main__":
    main()
