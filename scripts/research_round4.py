"""Frozen breadth hysteresis and volatility-sizing research; no independent holdout."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from scripts import research_round3 as previous

ROOT = PROJECT / "outputs/strategy-research-20261002-round4"
DOCS = PROJECT / "docs/research/2026-10-02-round4"
PLAN = PROJECT / "docs/superpowers/plans/2026-10-02-breadth-risk-research.md"
PERIODS = previous.PERIODS


def make_strategy(config):
    if config["kind"] == "breadth_risk":
        from scripts.research_round4_candidates import BreadthRiskStrategy
        return BreadthRiskStrategy(**config["params"])
    return previous.make_strategy(config)


def initialize(root):
    from scripts.research_round4_candidates import configurations, families
    path = root / "protocol.json"
    if path.exists():
        return previous.read_json(path)
    old = previous.read_json(previous.ROOT / "protocol.json")
    previous.verify(old)
    candidates = configurations()
    candidates.update({"breadth_original": {"kind": "round3", "params": {
        "family": "breadth_timing", "variant": 1, "gross_target": .8}},
        "breadth_static60": {"kind": "round3", "params": {
            "family": "breadth_timing", "variant": 1, "gross_target": .6}}})
    for config in candidates.values():
        make_strategy(config)
    sources = dict(old["source_files"])
    for source in (Path(__file__), PROJECT / "scripts/research_round4_candidates.py", PLAN):
        sources[source.relative_to(PROJECT).as_posix()] = previous.digest(source)
    protocol = {"purpose": "Approved follow-up: breadth hysteresis and basket volatility sizing; historical exploration",
                "periods": PERIODS, "candidates": candidates, "families": families(),
                "baselines": ["breadth_original", "breadth_static60"],
                "spec": old["spec"], "dataset": old["dataset"], "data_files": old["data_files"],
                "input_artifacts": old["input_artifacts"] | {
                    str(previous.ROOT / "protocol.json"): previous.digest(previous.ROOT / "protocol.json")},
                "source_files": sources, "data_checks": old["data_checks"], "limitations": old["limitations"],
                "starting_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
                "selection": {"score": "min(annual_return - 0.5 * max_drawdown) across development and validation",
                              "ties": "minimum early Sharpe, validation Sharpe, name",
                              "count": "one per new family, frozen before late/full",
                              "target": old["selection"]["target"],
                              "stress": "three selected plus original; full/late; all execution costs x2"}}
    for folder in (root, DOCS):
        previous.freeze(folder / "protocol.json", protocol)
    return protocol


def finite_sharpe(metrics):
    value = metrics["sharpe"]
    return value if value is not None and math.isfinite(value) else -math.inf


def choose(rows, families):
    lookup = previous.index_rows([r for r in rows if r["period"] in ("development", "validation")])
    def rank(name):
        early = [previous.metric(lookup, name, period) for period in ("development", "validation")]
        return (-min(previous.score(m) for m in early), -min(finite_sharpe(m) for m in early),
                -finite_sharpe(early[1]), name)
    return {family: min(names, key=rank) for family, names in families.items()}


def selection_value(root, protocol):
    rows = previous.evidence(root, protocol, ["development", "validation"])
    selected = choose(rows, protocol["families"])
    return {"protocol_sha256": previous.digest(root / "protocol.json"),
            "selected_by_family": selected, "selected": list(selected.values())}


def checked_selection(root, protocol):
    result = previous.read_json(root / "selection.json")
    if result != selection_value(root, protocol):
        raise ValueError("Frozen selection differs from early-period evidence")
    return result


def execute(root_text, name, period, cost):
    from quanto.backtest.dataset_loaders import load_dataset_for_run
    from quanto.backtest.engine import run_backtest
    from quanto.config import BacktestSpec
    from quanto.reports.backtest_report import build_report
    root = Path(root_text)
    protocol = previous.read_json(root / "protocol.json")
    previous.verify(protocol)
    expected = previous.identity(root, protocol, name, period, cost)
    folder = previous.destination(root, name, period, cost)
    cached = previous.load_cached(folder, expected)
    if cached is not None:
        previous.verify_metrics(folder, cached)
        return cached
    spec = BacktestSpec.model_validate(expected["spec"])
    bundle = load_dataset_for_run(protocol["dataset"], spec)
    started = time.perf_counter()
    result = run_backtest(bundle, make_strategy(expected["parameters"]), spec,
                          emit=lambda event: None, cancelled=lambda: False)
    report = build_report(result, bundle, spec)
    held = result.positions[result.positions.shares > 0]
    row = expected | {"metrics": report.metrics, "annual_returns": report.annual_returns.to_dict("records"),
                      "monthly_returns": report.monthly_returns.to_dict("records"),
                      "risk_event_count": len(result.risk_events),
                      "average_position_count": len(held) / len(result.equity),
                      "elapsed_seconds": time.perf_counter() - started}
    folder.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".round4-", dir=folder.parent) as temporary:
        staged = Path(temporary) / "result"
        staged.mkdir()
        report.curves.to_csv(staged / "curves.csv", index=False)
        result.equity.to_csv(staged / "equity.csv", index=False)
        result.fills.to_csv(staged / "fills.csv", index=False)
        result.positions.groupby("symbol").weight.mean().sort_values(ascending=False).to_csv(staged / "average_weights.csv")
        row["artifacts"] = {p.name: previous.digest(p) for p in staged.iterdir()}
        previous.write_json(staged / "summary.json", row)
        previous.verify_metrics(staged, row)
        previous.verify(protocol)
        staged.rename(folder)
    return row


def audit(root, protocol, selection):
    rows = previous.evidence(root, protocol, PERIODS)
    stressed = selection["selected"] + ["breadth_original"]
    rows += previous.evidence(root, protocol, ["full", "late"], stressed, 2)
    matches = []
    for row in rows:
        if row["candidate"] != "breadth_original":
            continue
        old_folder = previous.destination(previous.ROOT, "breadth_timing_v1", row["period"], row["cost_multiplier"])
        old = previous.read_json(old_folder / "summary.json")
        previous.load_cached(old_folder, {key: old[key] for key in (
            "candidate", "period", "cost_multiplier", "parameters", "spec", "protocol_sha256")})
        previous.verify_metrics(old_folder, old)
        if row["metrics"] != old["metrics"]:
            raise ValueError(f"Original baseline changed: {row['period']} cost{row['cost_multiplier']}")
        matches.append({"period": row["period"], "cost_multiplier": row["cost_multiplier"], "all_metrics_equal": True})
    previous.write_json(DOCS / "results.json", rows)
    pd.DataFrame([{"candidate": r["candidate"], "period": r["period"], "cost_multiplier": r["cost_multiplier"],
                   "average_position_count": r["average_position_count"], **r["metrics"]} for r in rows]).to_csv(
        DOCS / "results.csv", index=False, encoding="utf-8-sig")
    previous.verify(protocol)
    previous.write_json(DOCS / "verification.json", {
        "verified": True, "runs": len(rows), "protocol_sha256": previous.digest(root / "protocol.json"),
        "baseline_reproductions": matches, "results_sha256": previous.digest(DOCS / "results.json"),
        "checks": ["frozen data, implementation, design and identity hashes", "early-only selection unchanged",
                   "artifact hashes and curve/equity consistency", "12 return, exposure, risk and cost metrics independently reconciled"]})
    print(f"Verified {len(rows)} complete results and {len(matches)} exact original baseline replays", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--phase", required=True, choices=("initialize", "development", "validation", "select", "final", "stress", "audit"))
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = args.root.resolve()
    protocol = initialize(root)
    previous.verify(protocol)
    if args.phase == "initialize":
        print(f"Frozen {len(protocol['candidates'])} configurations at {root}")
        return
    if args.phase in ("validation", "select", "final", "stress", "audit"):
        previous.evidence(root, protocol, ["development"])
    if args.phase == "select":
        selected = selection_value(root, protocol)
        for folder in (root, DOCS):
            previous.freeze(folder / "selection.json", selected)
        print(previous.read_json(root / "selection.json"))
        return
    if args.phase in ("final", "stress", "audit"):
        selected = checked_selection(root, protocol)
    if args.phase == "audit":
        audit(root, protocol, selected)
        return
    if args.phase == "stress":
        names, periods, cost = selected["selected"] + ["breadth_original"], ["full", "late"], 2
    else:
        names, periods, cost = list(protocol["candidates"]), (["late", "full"] if args.phase == "final" else [args.phase]), 1
    jobs = [(name, period, cost) for period in periods for name in names]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(execute, str(root), *job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            print({"done": index, "total": len(jobs), "candidate": row["candidate"], "period": row["period"],
                   **{key: row["metrics"][key] for key in ("total_return", "annual_return", "max_drawdown", "sharpe")}}, flush=True)


if __name__ == "__main__":
    main()
