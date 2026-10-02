"""Frozen volatility-neighborhood research; all periods are retrospective."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import math
from pathlib import Path
import subprocess
import sys

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from scripts import research_round3 as previous
from scripts import research_round4

ROOT = PROJECT / "outputs/strategy-research-20261002-round5"
DOCS = PROJECT / "docs/research/2026-10-02-round5"
PLAN = PROJECT / "docs/superpowers/plans/2026-10-02-volatility-neighborhood-research.md"
BASELINE = "baseline"
MAIN_PERIODS = previous.PERIODS
RESTARTS = {
    "restart_20240701": ["2024-07-01", "2026-09-30"],
    "restart_20250701": ["2025-07-01", "2026-09-30"],
    "restart_20260401": ["2026-04-01", "2026-09-30"],
}
EARLY = ("development", "validation")


def configurations():
    baseline = {"entry_breadth": .55, "exit_breadth": .45,
                "target_vol": .12, "vol_window": 60, "gross_target": .8}
    candidates = {BASELINE: {"kind": "breadth_risk", "params": baseline.copy()}}
    for window in (30, 60, 90):
        for target in (.11, .12, .13):
            if window == 60 and target == .12:
                continue
            candidates[f"w{window}_v{round(target * 100)}"] = {
                "kind": "breadth_risk", "params": baseline | {"vol_window": window, "target_vol": target}}
    for window in (20, 120):
        candidates[f"w{window}_v12"] = {
            "kind": "breadth_risk", "params": baseline | {"vol_window": window}}
    for name, key, value in (("entry50", "entry_breadth", .50),
                             ("entry65", "entry_breadth", .65),
                             ("exit35", "exit_breadth", .35),
                             ("exit50", "exit_breadth", .50)):
        candidates[name] = {"kind": "breadth_risk", "params": baseline | {key: value}}
    return candidates


def initialize(root):
    path = root / "protocol.json"
    if path.exists():
        return previous.read_json(path)
    old_path = research_round4.ROOT / "protocol.json"
    old = previous.read_json(old_path)
    previous.verify(old)
    candidates = configurations()
    if len(candidates) != 15:
        raise ValueError("Exactly fifteen configurations required")
    for config in candidates.values():
        research_round4.make_strategy(config)
    sources = dict(old["source_files"])
    for source in (Path(__file__), PLAN):
        sources[source.relative_to(PROJECT).as_posix()] = previous.digest(source)
    protocol = {
        "purpose": "Volatility neighborhood of Round 4 h55_45_vol12; retrospective exploration",
        "periods": MAIN_PERIODS | RESTARTS, "candidates": candidates, "baselines": [BASELINE],
        "spec": old["spec"], "dataset": old["dataset"], "data_files": old["data_files"],
        "input_artifacts": old["input_artifacts"] | {str(old_path): previous.digest(old_path)},
        "source_files": sources, "data_checks": old["data_checks"],
        "limitations": old["limitations"],
        "starting_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
        "selection": {
            "score": "minimum early-period improvement over baseline in the same period: annual_return - 0.5 * max_drawdown",
            "ties": "minimum early Sharpe, validation Sharpe, ascending name; missing/nonfinite Sharpe last",
            "deduplication": "ordered development/validation curves.csv SHA256 pair; baseline pair excluded",
            "count": "up to three distinct paths, frozen before late/full",
            "target": old["selection"]["target"],
            "stress": "baseline plus selected; full/late at all execution costs x2; three normal-cost cash restarts"}}
    for folder in (root, DOCS):
        previous.freeze(folder / "protocol.json", protocol)
    return protocol


def finite_sharpe(metrics):
    value = metrics["sharpe"]
    return value if value is not None and math.isfinite(value) else -math.inf


def choose(rows, names):
    """Rank distinct early paths by worst same-period improvement over baseline."""
    early_rows = [row for row in rows if row["period"] in EARLY]
    lookup = previous.index_rows(early_rows)
    artifacts = {(row["candidate"], row["period"]): row["artifacts"]["curves.csv"]
                 for row in early_rows}
    baseline_scores = {period: previous.score(previous.metric(lookup, BASELINE, period))
                       for period in EARLY}

    def details(name):
        metrics = [previous.metric(lookup, name, period) for period in EARLY]
        hashes = tuple(artifacts[name, period] for period in EARLY)
        improvements = [previous.score(metric) - baseline_scores[period]
                        for metric, period in zip(metrics, EARLY)]
        if not all(math.isfinite(value) for value in improvements):
            raise ValueError(f"Nonfinite selection score: {name}")
        sharpes = [finite_sharpe(metric) for metric in metrics]
        minimum_sharpe = min(sharpes)
        return {"name": name, "curve_hashes": hashes,
                "minimum_improvement": min(improvements),
                "minimum_sharpe": minimum_sharpe if math.isfinite(minimum_sharpe) else None,
                "validation_sharpe": sharpes[1] if math.isfinite(sharpes[1]) else None}

    def rank(item):
        weakest = -math.inf if item["minimum_sharpe"] is None else item["minimum_sharpe"]
        validation = -math.inf if item["validation_sharpe"] is None else item["validation_sharpe"]
        return (-item["minimum_improvement"], -weakest, -validation, item["name"])

    if BASELINE not in names or len(names) != len(set(names)):
        raise ValueError("Candidate names must be unique and include baseline")
    ranked = sorted((details(name) for name in names), key=rank)
    baseline_pair = details(BASELINE)["curve_hashes"]
    baseline_aliases = [BASELINE] + [item["name"] for item in ranked
                                    if item["name"] != BASELINE and item["curve_hashes"] == baseline_pair]
    grouped = {}
    for item in ranked:
        pair = item["curve_hashes"]
        if pair == baseline_pair:
            continue
        if pair not in grouped:
            grouped[pair] = {"representative": item["name"], "aliases": [],
                             "curve_hashes": list(pair),
                             "minimum_improvement": item["minimum_improvement"],
                             "minimum_sharpe": item["minimum_sharpe"],
                             "validation_sharpe": item["validation_sharpe"]}
        grouped[pair]["aliases"].append(item["name"])
    groups = list(grouped.values())
    return {"selected": [group["representative"] for group in groups[:3]],
            "groups": groups, "baseline_aliases": baseline_aliases}


def selection_value(root, protocol):
    rows = previous.evidence(root, protocol, EARLY)
    return {"protocol_sha256": previous.digest(root / "protocol.json"),
            **choose(rows, list(protocol["candidates"]))}


def checked_selection(root, protocol):
    saved = previous.read_json(root / "selection.json")
    if saved != selection_value(root, protocol):
        raise ValueError("Frozen selection differs from early-period evidence")
    if previous.read_json(DOCS / "selection.json") != saved:
        raise ValueError("Published selection differs from frozen selection")
    return saved


def audit(root, protocol, selection):
    names = [BASELINE] + selection["selected"]
    rows = previous.evidence(root, protocol, MAIN_PERIODS)
    rows += previous.evidence(root, protocol, ("full", "late"), names, 2)
    rows += previous.evidence(root, protocol, RESTARTS, names)
    matches = []
    for period, cost in [(period, 1) for period in MAIN_PERIODS] + [("full", 2), ("late", 2)]:
        current = next(row for row in rows if row["candidate"] == BASELINE
                       and row["period"] == period and row["cost_multiplier"] == cost)
        old_folder = previous.destination(research_round4.ROOT, "h55_45_vol12", period, cost)
        old = previous.read_json(old_folder / "summary.json")
        previous.load_cached(old_folder, {key: old[key] for key in (
            "candidate", "period", "cost_multiplier", "parameters", "spec", "protocol_sha256")})
        previous.verify_metrics(old_folder, old)
        if current["metrics"] != old["metrics"]:
            raise ValueError(f"Round 4 baseline changed: {period} cost{cost}")
        matches.append({"period": period, "cost_multiplier": cost, "all_metrics_equal": True})
    expected_count = 60 + 5 * len(names)
    if len(rows) != expected_count or len(rows) > 80:
        raise ValueError("Unexpected research run count")
    previous.write_json(DOCS / "results.json", rows)
    pd.DataFrame([{"candidate": row["candidate"], "period": row["period"],
                   "cost_multiplier": row["cost_multiplier"],
                   "average_position_count": row["average_position_count"], **row["metrics"]}
                  for row in rows]).to_csv(DOCS / "results.csv", index=False, encoding="utf-8-sig")
    previous.verify(protocol)
    previous.write_json(DOCS / "verification.json", {
        "verified": True, "runs": len(rows),
        "protocol_sha256": previous.digest(root / "protocol.json"),
        "selection_sha256": previous.digest(root / "selection.json"),
        "baseline_reproductions": matches,
        "results_sha256": previous.digest(DOCS / "results.json"),
        "checks": ["frozen sources, data, inputs and identities",
                   "early selection and ordered curve-hash deduplication",
                   "artifact hashes and independently reconciled return, risk, exposure and cost metrics",
                   "six exact Round 4 baseline metric dictionaries"]})
    print(f"Verified {len(rows)} results and {len(matches)} exact Round 4 baseline replays", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--phase", required=True, choices=(
        "initialize", "development", "validation", "select", "final", "stress", "restarts", "audit"))
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = args.root.resolve()
    protocol = initialize(root)
    previous.verify(protocol)
    if args.phase == "initialize":
        print(f"Frozen {len(protocol['candidates'])} configurations at {root}")
        return
    if args.phase in ("validation", "select", "final", "stress", "restarts", "audit"):
        previous.evidence(root, protocol, ["development"])
    if args.phase in ("select", "final", "stress", "restarts", "audit"):
        previous.evidence(root, protocol, ["validation"])
    if args.phase == "select":
        selected = selection_value(root, protocol)
        for folder in (root, DOCS):
            previous.freeze(folder / "selection.json", selected)
        print(previous.read_json(root / "selection.json"))
        return
    if args.phase in ("final", "stress", "restarts", "audit"):
        selected = checked_selection(root, protocol)
    if args.phase == "audit":
        audit(root, protocol, selected)
        return
    if args.phase == "stress":
        names, periods, cost = [BASELINE] + selected["selected"], ("full", "late"), 2
    elif args.phase == "restarts":
        names, periods, cost = [BASELINE] + selected["selected"], RESTARTS, 1
    else:
        names = list(protocol["candidates"])
        periods = ("late", "full") if args.phase == "final" else (args.phase,)
        cost = 1
    jobs = [(name, period, cost) for period in periods for name in names]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(research_round4.execute, str(root), *job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            print({"done": index, "total": len(jobs), "candidate": row["candidate"],
                   "period": row["period"], **{key: row["metrics"][key] for key in
                   ("total_return", "annual_return", "max_drawdown", "sharpe")}}, flush=True)


if __name__ == "__main__":
    main()
