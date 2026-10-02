"""Frozen ten-family ETF research on the audited shared-cache snapshot.

Phases: initialize, development, select-training, validation, select, final,
stress, audit. Every selection is saved before calculating later returns.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
ROOT = PROJECT / "outputs/strategy-research-20261002-round3"
DOCS = PROJECT / "docs/research/2026-10-02-round3"
INPUT = PROJECT / "outputs/etf-expansion-20261002/shared-cache"
PERIODS = {
    "development": ["2023-10-09", "2024-12-31"],
    "validation": ["2025-01-02", "2025-12-31"],
    "late": ["2026-01-05", "2026-09-30"],
    "full": ["2023-10-09", "2026-09-30"],
}
BASELINES = ["trend_20_20_k3", "guarded_momentum_200_v0.1",
             "diversified_200_inverse_vol", "buy_hold", "cash_buy_hold_40",
             "group_equal_80", "group_equal_40"]


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


def freeze(path, value):
    if Path(path).exists() and read_json(path) != value:
        raise ValueError(f"Frozen artifact differs: {path}")
    write_json(path, value)


def make_strategy(config):
    from scripts.research_round3_candidates import Round3Strategy, GroupEqual
    from scripts.research_round2_candidates import DiversifiedTrend, CashBuyHold
    from scripts.research_rotation_candidates import GuardedRotation
    from quanto.strategies.buy_hold import BuyHold
    from quanto.strategies.trend_rotation import TrendRotation
    factory = {"round3": Round3Strategy, "group_equal": GroupEqual,
               "diversified_trend": DiversifiedTrend, "cash_buy_hold": CashBuyHold,
               "guarded_rotation": GuardedRotation, "buy_hold": BuyHold, "trend_rotation": TrendRotation}
    return factory[config["kind"]](**config["params"])


def initialize(root):
    from scripts.research_round3_candidates import configurations, descriptions
    from quanto.backtest.dataset_loaders import load_dataset
    path = root / "protocol.json"
    if path.exists():
        return read_json(path)
    previous = read_json(INPUT / "protocol.json")
    source_audit = read_json(INPUT / "source_audit.json")
    for name, expected in source_audit["data_files"].items():
        if digest(INPUT / "dataset" / name) != expected:
            raise ValueError("Audited input changed")
    bundle = load_dataset(INPUT / "dataset")
    selection = read_json(INPUT / "qualified_selection.json")
    symbols = sorted(row["symbol"] for row in selection["symbols"])
    if len(symbols) != 305 or symbols != sorted(bundle.instruments.symbol):
        raise ValueError("Expected the audited 305-ETF pool")
    candidates = configurations()
    families = {}
    for name, config in candidates.items():
        families.setdefault(config["params"]["family"], []).append(name)
        make_strategy(config)
    if len(families) != 10 or any(len(names) != 2 for names in families.values()):
        raise ValueError("Exactly ten families and two variants each required")
    candidates.update({name: previous["candidates"][name] for name in BASELINES[:5]})
    for gross in (.8, .4):
        candidates[f"group_equal_{gross * 100:.0f}"] = {"kind": "group_equal", "params": {"gross_target": gross}}
    sources = [path for path in (PROJECT / "src/quanto").rglob("*.py")
               if not {"api", "workbench"}.intersection(path.relative_to(PROJECT / "src/quanto").parts)]
    sources += [Path(__file__).resolve(), PROJECT / "scripts/research_round3_candidates.py",
                PROJECT / "scripts/research_round2_candidates.py", PROJECT / "scripts/research_rotation_candidates.py",
                PROJECT / "uv.lock", DOCS / "plan.md"]
    protocol = {
        "purpose": "Ten-family historical exploration; previously inspected periods are not independent holdouts",
        "periods": PERIODS, "candidates": candidates, "families": families,
        "descriptions": descriptions(), "baselines": BASELINES, "spec": previous["spec"],
        "dataset": str(INPUT / "dataset"), "data_files": source_audit["data_files"],
        "input_artifacts": {str(INPUT / name): digest(INPUT / name) for name in
                            ("source_audit.json", "qualified_selection.json", "protocol.json")},
        "source_files": {p.relative_to(PROJECT).as_posix(): digest(p) for p in sorted(sources)},
        "starting_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
        "selection": {"score": "annual_return - 0.5 * max_drawdown",
                      "training": "one variant per family by development score, Sharpe, name",
                      "shortlist": "top 3 training winners by min(development, validation) score, validation Sharpe, name",
                      "target": {"full_annual_return_min": .08, "full_max_drawdown_max": .15,
                                 "full_sharpe_min": .7, "all_three_periods_positive": True},
                      "stress": "top 3 plus group_equal_80 and group_equal_40; full/late; all costs x2"},
        "data_checks": {"symbols": len(symbols), "price_rows": len(bundle.prices),
                        "observed_dates": bundle.prices.date.nunique(),
                        "first": str(bundle.prices.date.min().date()), "last": str(bundle.prices.date.max().date())},
        "limitations": bundle.manifest.limitations,
    }
    freeze(path, protocol)
    freeze(DOCS / "protocol.json", protocol)
    return protocol


def verify(protocol):
    for name, expected in protocol["source_files"].items():
        if digest(PROJECT / name) != expected:
            raise ValueError(f"Frozen source changed: {name}")
    for name, expected in protocol["data_files"].items():
        if digest(Path(protocol["dataset"]) / name) != expected:
            raise ValueError(f"Frozen data changed: {name}")
    for name, expected in protocol["input_artifacts"].items():
        if digest(name) != expected:
            raise ValueError(f"Frozen input changed: {name}")


def identity(root, protocol, name, period, cost):
    from quanto.config import BacktestSpec
    raw = copy.deepcopy(protocol["spec"])
    raw["start"], raw["end"] = protocol["periods"][period]
    for key in ("commission_rate", "minimum_commission", "slippage_rate"):
        raw["execution"][key] *= cost
    spec = BacktestSpec.model_validate(raw)
    return {"candidate": name, "period": period, "cost_multiplier": cost,
            "parameters": protocol["candidates"][name], "spec": spec.model_dump(mode="json"),
            "protocol_sha256": digest(root / "protocol.json")}


def destination(root, name, period, cost=1):
    return root / "runs" / f"{period}__{name}__cost{cost:g}"


def load_cached(folder, expected):
    path = folder / "summary.json"
    if not path.exists():
        return None
    row = read_json(path)
    if any(row.get(key) != value for key, value in expected.items()):
        raise ValueError(f"Cached identity mismatch: {folder}")
    if not row.get("artifacts"):
        raise ValueError(f"Cached artifact manifest missing: {folder}")
    for name, value in row["artifacts"].items():
        if not (folder / name).is_file() or digest(folder / name) != value:
            raise ValueError(f"Cached artifact changed: {folder / name}")
    return row


def verify_metrics(folder, row):
    curves = pd.read_csv(folder / "curves.csv")
    equity = pd.read_csv(folder / "equity.csv")
    fills = pd.read_csv(folder / "fills.csv")
    cash = row["spec"]["initial_cash"]
    values = curves.equity.to_numpy(float)
    if (curves.date.duplicated().any() or not curves.date.is_monotonic_increasing
            or curves.date.iloc[0] != row["spec"]["start"] or curves.date.iloc[-1] != row["spec"]["end"]
            or not np.isfinite(values).all() or len(values) < 2):
        raise ValueError("Invalid curve dates or values")
    returns = values / np.r_[cash, values[:-1]] - 1
    peaks = np.maximum.accumulate(np.r_[cash, values])[1:]
    std = returns.std(ddof=0)
    annual = row["spec"]["metrics"]["annual_sessions"]
    calculated = {"final_equity": values[-1], "total_return": values[-1] / cash - 1,
                  "annual_return": (values[-1] / cash) ** (annual / len(values)) - 1,
                  "max_drawdown": max(1 - values / peaks), "volatility": std * np.sqrt(annual),
                  "sharpe": float((returns.mean() - row["spec"]["metrics"]["risk_free_rate"] / annual)
                                  / std * np.sqrt(annual)) if std > 0 else None,
                  "average_invested_ratio": float((equity.market_value / equity.equity).mean()),
                  "commission_paid": float(fills.commission.sum()), "slippage_cost": float(fills.slippage_cost.sum()),
                  "execution_cost": float(fills.commission.sum() + fills.slippage_cost.sum()),
                  "fill_count": len(fills)}
    for key, expected in calculated.items():
        actual = row["metrics"][key]
        if expected is None and actual is None:
            continue
        if actual is None or not np.isclose(actual, expected, rtol=1e-10, atol=1e-10):
            raise ValueError(f"Metric reconciliation failed: {row['candidate']} {key}: {actual} != {expected}")
    if not np.allclose(equity.equity, values, rtol=1e-12, atol=1e-8):
        raise ValueError("Equity and curve disagree")
    return len(values)


def execute(root_text, name, period, cost):
    from quanto.backtest.dataset_loaders import load_dataset_for_run
    from quanto.backtest.engine import run_backtest
    from quanto.config import BacktestSpec
    from quanto.reports.backtest_report import build_report
    root = Path(root_text)
    protocol = read_json(root / "protocol.json")
    verify(protocol)
    expected = identity(root, protocol, name, period, cost)
    folder = destination(root, name, period, cost)
    cached = load_cached(folder, expected)
    if cached is not None:
        verify_metrics(folder, cached)
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
    with tempfile.TemporaryDirectory(prefix=".round3-", dir=folder.parent) as temporary:
        staged = Path(temporary) / "result"
        staged.mkdir()
        report.curves.to_csv(staged / "curves.csv", index=False)
        result.equity.to_csv(staged / "equity.csv", index=False)
        result.fills.to_csv(staged / "fills.csv", index=False)
        # Include zero-position days in the denominator, not only held dates.
        result.positions.groupby("symbol").weight.mean().sort_values(ascending=False).to_csv(staged / "average_weights.csv")
        row["artifacts"] = {p.name: digest(p) for p in staged.iterdir()}
        write_json(staged / "summary.json", row)
        verify_metrics(staged, row)
        verify(protocol)
        staged.rename(folder)
    return row


def evidence(root, protocol, periods, names=None, cost=1):
    rows = []
    for period in periods:
        for name in names or protocol["candidates"]:
            folder = destination(root, name, period, cost)
            row = load_cached(folder, identity(root, protocol, name, period, cost))
            if row is None:
                raise ValueError(f"Missing result: {folder}")
            verify_metrics(folder, row)
            rows.append(row)
    return rows


def index_rows(rows):
    lookup = {}
    for row in rows:
        key = row["candidate"], row["period"]
        if key in lookup:
            raise ValueError(f"Duplicate result: {key}")
        lookup[key] = row["metrics"]
    return lookup


def metric(lookup, name, period):
    if (name, period) not in lookup:
        raise ValueError(f"Missing result: {name} {period}")
    return lookup[name, period]


def score(metrics):
    return metrics["annual_return"] - .5 * metrics["max_drawdown"]


def sharpe(metrics):
    return metrics["sharpe"] if metrics["sharpe"] is not None else -float("inf")


def choose_training(rows, families):
    lookup = index_rows(rows)
    return {family: sorted(names, key=lambda name: (
        -score(metric(lookup, name, "development")), -sharpe(metric(lookup, name, "development")), name))[0]
            for family, names in families.items()}


def choose_shortlist(rows, selected):
    lookup = index_rows(rows)
    return sorted(selected.values(), key=lambda name: (
        -min(score(metric(lookup, name, p)) for p in ("development", "validation")),
        -sharpe(metric(lookup, name, "validation")), name))[:3]


def checked_training(root, protocol):
    saved = read_json(root / "training_selection.json")
    rows = evidence(root, protocol, ["development"])
    if saved != {"protocol_sha256": digest(root / "protocol.json"),
                 "selected_by_family": choose_training(rows, protocol["families"])}:
        raise ValueError("Training selection mismatch")
    return saved


def checked_selection(root, protocol):
    training = checked_training(root, protocol)
    saved = read_json(root / "selection.json")
    rows = evidence(root, protocol, ["development", "validation"])
    if saved != {"protocol_sha256": digest(root / "protocol.json"),
                 "training_selection_sha256": digest(root / "training_selection.json"),
                 "selected": choose_shortlist(rows, training["selected_by_family"])}:
        raise ValueError("Shortlist mismatch")
    return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--phase", required=True, choices=("initialize", "development", "select-training",
                        "validation", "select", "final", "stress", "audit"))
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    root = args.root.resolve()
    protocol = initialize(root)
    verify(protocol)
    if args.phase == "initialize":
        print(f"Frozen {len(protocol['candidates'])} configurations at {root}")
        return
    if args.phase == "select-training":
        rows = evidence(root, protocol, ["development"])
        result = {"protocol_sha256": digest(root / "protocol.json"),
                  "selected_by_family": choose_training(rows, protocol["families"])}
        for path in (root, DOCS):
            freeze(path / "training_selection.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.phase in ("validation", "select", "final", "stress", "audit"):
        training = checked_training(root, protocol)
    if args.phase == "select":
        rows = evidence(root, protocol, ["development", "validation"])
        result = {"protocol_sha256": digest(root / "protocol.json"),
                  "training_selection_sha256": digest(root / "training_selection.json"),
                  "selected": choose_shortlist(rows, training["selected_by_family"])}
        for path in (root, DOCS):
            freeze(path / "selection.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.phase in ("final", "stress", "audit"):
        selection = checked_selection(root, protocol)
    if args.phase == "audit":
        rows = evidence(root, protocol, PERIODS)
        rows += evidence(root, protocol, ["full", "late"], selection["selected"] + ["group_equal_80", "group_equal_40"], 2)
        output = [{"candidate": r["candidate"], "period": r["period"], "cost_multiplier": r["cost_multiplier"],
                   "average_position_count": r["average_position_count"], **r["metrics"]} for r in rows]
        write_json(DOCS / "results.json", rows)
        pd.DataFrame(output).to_csv(DOCS / "results.csv", index=False, encoding="utf-8-sig")
        verify(protocol)
        write_json(DOCS / "verification.json", {"verified": True, "runs": len(rows),
                   "protocol_sha256": digest(root / "protocol.json"),
                   "checks": ["source, data, audit and parameter hashes", "selection rules and permitted periods",
                              "result identities and artifact hashes", "12 equity, risk, exposure and cost metrics recomputed"],
                   "results_sha256": digest(DOCS / "results.json")})
        print(f"Verified {len(rows)} complete results")
        return
    if args.phase == "stress":
        names, periods, cost = selection["selected"] + ["group_equal_80", "group_equal_40"], ["full", "late"], 2
    else:
        names = list(protocol["candidates"])
        periods = ["late", "full"] if args.phase == "final" else [args.phase]
        cost = 1
    jobs = [(name, period, cost) for period in periods for name in names]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(execute, str(root), *job) for job in jobs]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            print(json.dumps({"done": index, "total": len(jobs), "candidate": row["candidate"],
                  "period": row["period"], **{key: row["metrics"][key] for key in
                  ("total_return", "annual_return", "max_drawdown", "sharpe", "execution_cost")}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
