"""Frozen second-round ETF study; historical exploration, not a fresh holdout."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

from quanto.backtest.engine import run_backtest
from quanto.config import BacktestSpec
from quanto.backtest.dataset_loaders import load_dataset_for_run
from quanto.reports.backtest_report import build_report
from quanto.strategies.buy_hold import BuyHold
from quanto.strategies.trend_rotation import TrendRotation
from research_rotation_candidates import GuardedRotation
from research_round2_candidates import BreadthRotation, CashBuyHold, CoreSatellite, DiversifiedTrend


PROJECT = Path(__file__).resolve().parents[1]
PREVIOUS = PROJECT / "outputs/strategy-research-20261001"
DEFAULT_ROOT = PROJECT / "outputs/strategy-research-20261001-round2"
PERIODS = {
    "early": ["2020-01-02", "2022-12-30"],
    "middle": ["2023-01-03", "2024-12-31"],
    "recent": ["2025-01-02", "2026-05-19"],
    "full": ["2020-01-02", "2026-05-19"],
}
BASELINES = ["trend_20_20_k3", "guarded_momentum_200_v0.1", "buy_hold", "cash_buy_hold_40", "cash_buy_hold_60"]
STRATEGIES = {
    "trend_rotation": TrendRotation, "guarded_rotation": GuardedRotation,
    "buy_hold": BuyHold, "cash_buy_hold": CashBuyHold,
    "diversified_trend": DiversifiedTrend, "core_satellite": CoreSatellite,
    "breadth_rotation": BreadthRotation,
}


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candidates() -> dict:
    previous = json.loads((PREVIOUS / "protocol.json").read_text(encoding="utf-8"))
    grid = {name: previous["candidates"][name] for name in BASELINES[:3]}
    for gross in (.4, .6):
        grid[f"cash_buy_hold_{gross * 100:.0f}"] = {"kind": "cash_buy_hold", "params": {"gross_target": gross}}
    for window in (120, 200):
        for weighting in ("equal", "inverse_vol"):
            grid[f"diversified_{window}_{weighting}"] = {
                "kind": "diversified_trend", "params": {"trend_window": window, "gross_target": .8, "weighting": weighting}}
        for bear in (0., .2):
            grid[f"breadth_{window}_bear{bear * 100:.0f}"] = {
                "kind": "breadth_rotation", "params": {"trend_window": window, "bear_budget": bear}}
    for core in (.4, .6):
        for window in (20, 60):
            grid[f"core{core * 100:.0f}_momentum{window}"] = {
                "kind": "core_satellite", "params": {"core_weight": core, "momentum_window": window}}
    return grid


def initialize(root: Path) -> dict:
    path = root / "protocol.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    previous = json.loads((PREVIOUS / "protocol.json").read_text(encoding="utf-8"))
    sources = [*sorted((PROJECT / "src/quanto").rglob("*.py")), PROJECT / "uv.lock",
               PROJECT / "scripts/research_rotation_candidates.py", Path(__file__).resolve(),
               PROJECT / "scripts/research_round2_candidates.py", PROJECT / "docs/research/2026-10-01-round2/plan.md"]
    protocol = {
        "purpose": "Second-round historical exploration after observing first-round results; no independent holdout claim",
        "periods": PERIODS, "candidates": candidates(), "baselines": BASELINES,
        "spec": previous["spec"], "dataset": (PREVIOUS / "dataset").relative_to(PROJECT).as_posix(),
        "data_files": previous["data_files"],
        "audit": (PREVIOUS / "source_audit.json").relative_to(PROJECT).as_posix(),
        "audit_sha256": previous["source_audit_sha256"],
        "source_files": {p.relative_to(PROJECT).as_posix(): digest(p) for p in sources},
        "starting_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
        "selection": {"ranking": "minimum of early/middle/recent annual_return - 0.5 * max_drawdown; ties by full Sharpe then name",
                      "top_count": 3, "target": {"full_annual_return_min": .08, "full_max_drawdown_max": .20,
                      "full_sharpe_min": .7, "each_subperiod_annual_return_positive": True},
                      "stress": "Top 3 new candidates plus trend_20_20_k3 and buy_hold, full/recent, all costs doubled"},
    }
    write_json(path, protocol)
    return protocol


def verify(protocol: dict) -> None:
    for name, expected in protocol["source_files"].items():
        if digest(PROJECT / name) != expected:
            raise ValueError(f"Frozen source changed: {name}")
    for name, expected in protocol["data_files"].items():
        if digest(PROJECT / protocol["dataset"] / name) != expected:
            raise ValueError(f"Frozen dataset changed: {name}")
    if digest(PROJECT / protocol["audit"]) != protocol["audit_sha256"]:
        raise ValueError("Source audit changed")


def load_cached(destination: Path, expected: dict) -> dict | None:
    path = destination / "summary.json"
    if not path.exists():
        return None
    row = json.loads(path.read_text(encoding="utf-8"))
    if any(row.get(key) != value for key, value in expected.items()):
        raise ValueError(f"Cached result identity or protocol mismatch: {destination}")
    curve = destination / "curves.csv"
    if not curve.is_file() or row.get("curves_sha256") != digest(curve):
        raise ValueError(f"Cached curve is missing or changed: {destination}")
    return row


def execute(root_text: str, identity: str, period: str, multiplier: float) -> dict:
    root = Path(root_text)
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    destination = root / "runs" / f"{period}__{identity}__cost{multiplier:g}"
    config = protocol["candidates"][identity]
    raw = protocol["spec"]
    raw["start"], raw["end"] = protocol["periods"][period]
    for key in ("commission_rate", "minimum_commission", "slippage_rate"):
        raw["execution"][key] *= multiplier
    spec = BacktestSpec.model_validate(raw)
    expected = {"candidate": identity, "period": period, "cost_multiplier": multiplier,
                "parameters": config, "spec": spec.model_dump(mode="json"),
                "protocol_sha256": digest(root / "protocol.json")}
    cached = load_cached(destination, expected)
    if cached is not None:
        return cached
    bundle = load_dataset_for_run(PROJECT / protocol["dataset"], spec)
    strategy = STRATEGIES[config["kind"]](**config["params"])
    started = time.perf_counter()
    result = run_backtest(bundle, strategy, spec, emit=lambda event: None, cancelled=lambda: False)
    report = build_report(result, bundle, spec)
    row = {**expected, "metrics": report.metrics,
           "annual_returns": report.annual_returns.to_dict("records"),
           "risk_event_count": len(result.risk_events), "elapsed_seconds": time.perf_counter() - started}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".round2-", dir=destination.parent) as folder:
        staged = Path(folder) / "result"
        staged.mkdir()
        report.curves.to_csv(staged / "curves.csv", index=False)
        row["curves_sha256"] = digest(staged / "curves.csv")
        write_json(staged / "summary.json", row)
        staged.rename(destination)
    return row


def select(root: Path, protocol: dict) -> dict:
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "runs").glob("*__cost1/summary.json"))]
    lookup = {(row["candidate"], row["period"]): row for row in rows}
    required = {(name, period) for name in protocol["candidates"] for period in PERIODS}
    if set(lookup) != required:
        raise ValueError(f"Complete the frozen grid first ({len(lookup)}/{len(required)})")
    if any(row["protocol_sha256"] != digest(root / "protocol.json") for row in rows):
        raise ValueError("Run results belong to a different protocol")
    ranking = []
    for name in protocol["candidates"]:
        full = lookup[name, "full"]["metrics"]
        parts = [lookup[name, period]["metrics"] for period in ("early", "middle", "recent")]
        ranking.append({"candidate": name, "baseline": name in BASELINES,
                        "worst_period_score": min(m["annual_return"] - .5 * m["max_drawdown"] for m in parts),
                        "full_annual_return": full["annual_return"], "full_max_drawdown": full["max_drawdown"],
                        "full_sharpe": full["sharpe"], "target_passed": full["annual_return"] >= .08
                        and full["max_drawdown"] <= .20 and full["sharpe"] >= .7
                        and all(m["annual_return"] > 0 for m in parts)})
    ranking.sort(key=lambda row: (-row["worst_period_score"], -row["full_sharpe"], row["candidate"]))
    selection = {"selected": [row["candidate"] for row in ranking if not row["baseline"]][:3],
                 "ranking": ranking, "protocol_sha256": digest(root / "protocol.json"),
                 "note": "Historical diagnostic shortlist; selection does not mean the target passed."}
    path = root / "selection.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != selection:
        raise ValueError("Cannot replace frozen selection")
    write_json(path, selection)
    return selection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--phase", choices=["initialize", "grid", "select", "stress"], required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = args.root.resolve()
    protocol = initialize(root)
    verify(protocol)
    if args.phase == "initialize":
        print(f"Frozen {len(protocol['candidates'])} configurations: {root / 'protocol.json'}")
        return
    if args.phase == "select":
        print(json.dumps(select(root, protocol), ensure_ascii=True, indent=2))
        return
    if args.phase == "grid":
        jobs = [(name, period, 1.) for period in PERIODS for name in protocol["candidates"]]
    else:
        selection = json.loads((root / "selection.json").read_text(encoding="utf-8"))
        if selection["protocol_sha256"] != digest(root / "protocol.json"):
            raise ValueError("Selection protocol mismatch")
        names = selection["selected"] + ["trend_20_20_k3", "buy_hold"]
        jobs = [(name, period, 2.) for period in ("recent", "full") for name in names]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(execute, str(root), *job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            print(json.dumps({"completed": index, "total": len(jobs), "candidate": row["candidate"],
                              "period": row["period"], **{key: row["metrics"][key] for key in
                              ("annual_return", "max_drawdown", "sharpe", "average_invested_ratio")}}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
