"""Finite, chronological parameter study using the unchanged Quanto ledger.

Research artifacts only: this script does not change application defaults or
submit trades. Run prepare_strategy_research_data.py before this script.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
import tempfile
import time

import pandas as pd

from quanto.backtest.engine import run_backtest
from quanto.config import BacktestSpec
from quanto.backtest.dataset_loaders import load_dataset_for_run
from quanto.reports.backtest_report import build_report, write_backtest_outputs
from quanto.strategies.buy_hold import BuyHold
from quanto.strategies.trend_rotation import TrendRotation
from research_rotation_candidates import GuardedRotation


PERIODS = {
    "development": ("2020-01-02", "2022-12-30"),
    "validation": ("2023-01-03", "2024-12-31"),
    "holdout": ("2025-01-02", "2026-05-19"),
    "full": ("2020-01-02", "2026-05-19"),
}
WINDOWS = [(20, 20), (60, 60), (120, 120), (200, 200), (120, 60), (200, 60)]


def json_write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def candidates() -> dict[str, dict]:
    grid = {}
    for trend, momentum in WINDOWS:
        for count in (3, 5):
            grid[f"trend_{trend}_{momentum}_k{count}"] = {
                "kind": "trend_rotation",
                "params": {"trend_window": trend, "momentum_window": momentum,
                           "entry_rank": count, "retain_rank": count + 2, "evaluation": "weekly"},
            }
    for trend in (120, 200):
        for selection in ("momentum", "pullback"):
            for volatility in (0.10, 0.15):
                grid[f"guarded_{selection}_{trend}_v{volatility:g}"] = {
                    "kind": "guarded_rotation",
                    "params": {"trend_window": trend, "momentum_window": 60,
                               "selection": selection, "target_volatility": volatility, "entry_rank": 3},
                }
    grid["buy_hold"] = {"kind": "buy_hold", "params": {}}
    return grid


def initialize(root: Path) -> dict:
    path = root / "protocol.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    audit = json.loads((root / "source_audit.json").read_text(encoding="utf-8"))
    instruments = pd.read_parquet(root / "dataset" / "instruments.parquet")
    groups = sorted(instruments["group"].unique())
    protocol = {
        "purpose": "收益与回撤平衡；有限搜索，后续时间段验证，不承诺收益",
        "periods": PERIODS,
        "candidates": candidates(),
        "baseline": "trend_20_20_k3",
        "development_amendment": "After seeing only the original 12 development results, add 8 guarded candidates. No validation or holdout results had been viewed. Preserve the pilot results; repeat the original grid to verify reproducibility.",
        "selection": {
            "development_shortlist": 3,
            "score": "annual_return - 0.5 * max_drawdown",
            "procedure": "Development top 3 only; validate those plus fixed baselines; choose highest validation score among finalists; freeze before holdout.",
            "acceptance": "Holdout annual return > 0, Sharpe >= 0.7, max drawdown <= 0.20; compare with same-risk baseline. These are research gates, not guarantees.",
            "stress": "Double commission, minimum commission and slippage; no retuning after holdout.",
        },
        "spec": {
            "start": PERIODS["development"][0], "end": PERIODS["development"][1],
            "symbols": sorted(instruments.symbol.tolist()), "initial_cash": 1_000_000, "seed": 7,
            "execution": {"commission_rate": 0.0002, "minimum_commission": 5,
                          "slippage_rate": 0.0005, "max_stale_sessions": 0, "model": "next_open"},
            "risk": {"max_total_weight": 0.8, "max_symbol_weight": 0.35,
                     "group_caps": {group: 0.5 for group in groups}, "stress_scenarios": [],
                     "drawdown_pause": None, "pause_action": "block_new", "recovery_mode": "never"},
            "metrics": {"annual_sessions": 252, "risk_free_rate": 0, "cash_rate": 0, "definition_version": "1"},
        },
        "source_audit_sha256": hashlib.sha256((root / "source_audit.json").read_bytes()).hexdigest(),
        "data_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((root / "dataset").iterdir()) if p.is_file()},
        "engine_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "runtime": {"python": platform.python_version(), **{name: version(name) for name in ("numpy", "pandas", "pyarrow", "pydantic")}},
        "source_files": {p.relative_to(Path.cwd()).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in [*sorted(Path.cwd().joinpath("src/quanto").rglob("*.py")),
                                   *sorted(Path.cwd().joinpath("scripts").glob("*research*.py")),
                                   Path.cwd() / "uv.lock"]},
        "limitations": ["All periods are historical and may have been seen previously; holdout means not used in this search.",
                        "Only cached, surviving ETFs are available; universe selection is not point-in-time complete.",
                        "Each period starts in cash with prior history retained; it is not a continuous walk-forward portfolio.",
                        "BuyHold buys eligible instruments at the first session only; later listings are not added.",
                        "Risk caps are enforced daily by the current engine, including drift reductions.",
                        "No automatic drawdown pause: existing never-resume mode would change the research question."],
    }
    # Read the audit before freezing the protocol; keep its complete content separately.
    if not audit:
        raise ValueError("Missing source audit")
    json_write(path, protocol)
    return protocol


def execute(root_text: str, period: str, identity: str, multiplier: float, export: bool) -> dict:
    root = Path(root_text)
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    config = protocol["candidates"][identity]
    raw_spec = protocol["spec"]
    raw_spec["start"], raw_spec["end"] = protocol["periods"][period]
    for name in ("commission_rate", "minimum_commission", "slippage_rate"):
        raw_spec["execution"][name] *= multiplier
    spec = BacktestSpec.model_validate(raw_spec)
    bundle = load_dataset_for_run(root / "dataset", spec)
    strategy_type = {"trend_rotation": TrendRotation, "guarded_rotation": GuardedRotation, "buy_hold": BuyHold}[config["kind"]]
    strategy = strategy_type(**config["params"])
    started = time.perf_counter()
    result = run_backtest(bundle, strategy, spec, emit=lambda event: None, cancelled=lambda: False)
    report = build_report(result, bundle, spec)
    outcome = {"candidate": identity, "period": period, "cost_multiplier": multiplier,
               "parameters": config, "spec": spec.model_dump(mode="json"), "metrics": report.metrics,
               "annual_returns": report.annual_returns.to_dict("records"),
               "risk_event_count": len(result.risk_events), "elapsed_seconds": time.perf_counter() - started}
    outcome["score"] = report.metrics["annual_return"] - 0.5 * report.metrics["max_drawdown"]
    destination = root / "runs" / f"{period}__{identity}__cost{multiplier:g}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".research-", dir=destination.parent) as staging:
        staged = Path(staging) / "result"
        staged.mkdir()
        report.curves.to_csv(staged / "curves.csv", index=False)
        report.annual_returns.to_csv(staged / "annual_returns.csv", index=False)
        if export:
            write_backtest_outputs(staged / "report", result, report)
        json_write(staged / "summary.json", outcome)
        staged.rename(destination)
    return outcome


def load_outcomes(root: Path, period: str, multiplier: float = 1) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((root / "runs").glob(f"{period}__*__cost{multiplier:g}/summary.json"))]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--phase", choices=["initialize", "development", "validation", "freeze", "holdout", "full", "stress"], required=True)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = args.root.resolve()
    protocol = initialize(root)
    if hashlib.sha256((root / "source_audit.json").read_bytes()).hexdigest() != protocol["source_audit_sha256"]:
        raise ValueError("Source audit changed after protocol freeze")
    for name, digest in protocol["source_files"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Research source changed after protocol freeze: {name}")
    for name, digest in protocol["data_files"].items():
        if hashlib.sha256((root / "dataset" / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Dataset changed after protocol freeze: {name}")
    phase, multiplier = args.phase, 1.0
    if phase == "initialize":
        print(root / "protocol.json")
        return
    if phase == "development":
        identities = list(protocol["candidates"])
    elif phase == "validation":
        development = load_outcomes(root, "development")
        if len(development) != len(protocol["candidates"]):
            raise ValueError("Complete the frozen development grid first")
        ranked = sorted((row for row in development if row["candidate"] != "buy_hold"),
                        key=lambda row: (-row["score"], row["candidate"]))[:3]
        identities = sorted({row["candidate"] for row in ranked} | {protocol["baseline"], "buy_hold"})
        shortlist = {"development_finalists": [row["candidate"] for row in ranked], "validation_candidates": identities}
        path = root / "shortlist.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != shortlist:
            raise ValueError("Cannot replace frozen shortlist")
        json_write(path, shortlist)
    elif phase == "freeze":
        shortlist = json.loads((root / "shortlist.json").read_text(encoding="utf-8"))
        validation = load_outcomes(root, "validation")
        if {row["candidate"] for row in validation} != set(shortlist["validation_candidates"]):
            raise ValueError("Complete the validation shortlist first")
        ranked = sorted((row for row in validation if row["candidate"] in shortlist["development_finalists"]),
                        key=lambda row: (-row["score"], row["candidate"]))
        choice = {"selected": ranked[0]["candidate"], "validation_score": ranked[0]["score"],
                  "reason": protocol["selection"]["procedure"], "validation_ranking": [row["candidate"] for row in ranked]}
        path = root / "selection.json"
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != choice:
            raise ValueError("Cannot replace frozen selection")
        json_write(path, choice)
        print(json.dumps(choice, ensure_ascii=False))
        return
    else:
        choice = json.loads((root / "selection.json").read_text(encoding="utf-8"))
        identities = sorted({choice["selected"], protocol["baseline"], "buy_hold"})
        if phase in ("full", "stress") and {row["candidate"] for row in load_outcomes(root, "holdout")} != set(identities):
            raise ValueError("Complete the frozen holdout comparison before stress or full-period diagnostics")
        if phase == "stress":
            phase, multiplier = "holdout", 2.0
    jobs = [identity for identity in identities
            if not (root / "runs" / f"{phase}__{identity}__cost{multiplier:g}" / "summary.json").exists()]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(execute, str(root), phase, identity, multiplier, phase in ("holdout", "full")): identity for identity in jobs}
        for future in as_completed(futures):
            row = future.result()
            m = row["metrics"]
            print(json.dumps({"candidate": row["candidate"], "period": phase,
                              **{name: m[name] for name in ("annual_return", "max_drawdown", "sharpe", "average_invested_ratio")},
                              "score": row["score"], "seconds": round(row["elapsed_seconds"], 1)}, ensure_ascii=False), flush=True)
    print(f"Completed {phase}: {len(identities)} candidates; cost multiplier {multiplier:g}", flush=True)


if __name__ == "__main__":
    main()
