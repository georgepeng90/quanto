"""Post-hoc execution sensitivity for the frozen round-three breadth strategy."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.research_round3 import (
    DOCS, ROOT, destination, digest, identity, load_cached, make_strategy,
    read_json, verify, verify_metrics, write_json,
)
from quanto.backtest.dataset_loaders import load_dataset_for_run
from quanto.backtest.engine import run_backtest
from quanto.config import BacktestSpec
from quanto.reports.backtest_report import build_report


NAME = "breadth_timing_v1"
CAPITALS = (990_000, 1_010_000)
PERIODS = ("full", "late")
TRACE_DATE = "2026-05-22"


def replay(root: Path, protocol: dict, period: str, capital: int, *, cost: int = 1,
           folder: Path | None = None) -> dict:
    raw = identity(root, protocol, NAME, period, cost)["spec"]
    raw["initial_cash"] = capital
    spec = BacktestSpec.model_validate(raw)
    bundle = load_dataset_for_run(protocol["dataset"], spec)
    captured = {}
    signal_targets = {}

    def emit(event):
        signal = event.payload["tables"]["signals"][0]
        if signal["intent"] == "target":
            signal_targets[event.asof] = signal["weights"]
        if event.asof == TRACE_DATE:
            captured.update({"signal": signal,
                             "risk_events": event.payload["tables"]["risk_events"],
                             "orders": event.payload["tables"]["orders"]})

    result = run_backtest(bundle, make_strategy(protocol["candidates"][NAME]), spec,
                          emit=emit, cancelled=lambda: False)
    report = build_report(result, bundle, spec)
    item = {"period": period, "initial_cash": capital, "cost_multiplier": cost,
            "metrics": report.metrics, "trace": captured,
            "signal_targets": signal_targets}
    if folder is not None:
        folder.mkdir(parents=True, exist_ok=True)
        report.curves.to_csv(folder / "curves.csv", index=False)
        result.equity.to_csv(folder / "equity.csv", index=False)
        result.fills.to_csv(folder / "fills.csv", index=False)
        row = {"candidate": NAME, "spec": spec.model_dump(mode="json"),
               "metrics": report.metrics}
        verify_metrics(folder, row)
        item["curve_sha256"] = digest(folder / "curves.csv")
        item["artifacts"] = {name: digest(folder / name) for name in
                             ("curves.csv", "equity.csv", "fills.csv")}
        write_json(folder / "summary.json", {k: v for k, v in item.items() if k != "signal_targets"})
    return item


def baseline(root: Path, protocol: dict, period: str, cost: int) -> dict:
    folder = destination(root, NAME, period, cost)
    row = load_cached(folder, identity(root, protocol, NAME, period, cost))
    if row is None:
        raise ValueError(f"Missing frozen run: {folder}")
    verify_metrics(folder, row)
    return row


def compare_saved_paths(root: Path) -> dict:
    folders = [destination(root, NAME, "late", cost) for cost in (1, 2)]
    equities = [pd.read_csv(folder / "equity.csv") for folder in folders]
    first_equity = equities[0].merge(equities[1], on="date", suffixes=("_1", "_2"))
    first_equity = first_equity.loc[~np.isclose(first_equity.equity_1, first_equity.equity_2,
                                                rtol=0, atol=1e-8)]
    fills = [pd.read_csv(folder / "fills.csv", dtype={"symbol": str}) for folder in folders]
    quantities = []
    for frame in fills:
        frame["signed_quantity"] = frame.quantity * frame.side.map({"buy": 1, "sell": -1})
        quantities.append(frame.groupby(["trade_date", "symbol"]).signed_quantity.sum())
    changes = quantities[0].rename("cost1").to_frame().join(quantities[1].rename("cost2"), how="outer").fillna(0)
    changes = changes.loc[changes.cost1 != changes.cost2]
    if first_equity.empty or changes.empty:
        raise ValueError("Expected cost paths to diverge")
    first_quantity_date = changes.index.get_level_values("trade_date").min()
    day = TRACE_DATE
    invested = {f"cost{cost}": float(frame.set_index("date").loc[day, "market_value"] /
                                    frame.set_index("date").loc[day, "equity"])
                for cost, frame in zip((1, 2), equities)}
    trade_date = "2026-05-25"
    key_symbols = ("159967", "562500")
    trades = {f"cost{cost}": frame.loc[(frame.trade_date == trade_date)
                                       & frame.symbol.isin(key_symbols),
                                       ["symbol", "side", "quantity", "price"]].to_dict("records")
              for cost, frame in zip((1, 2), fills)}
    if not (invested["cost1"] > .8 > invested["cost2"]
            and any(row["symbol"] == "159967" for row in trades["cost2"])
            and not any(row["symbol"] == "159967" for row in trades["cost1"])):
        raise ValueError("Expected capital-boundary trade path is absent")
    return {"first_equity_divergence": first_equity.iloc[0].date,
            "first_quantity_divergence": first_quantity_date,
            "first_quantity_differences": changes.loc[first_quantity_date].reset_index().to_dict("records"),
            "trigger_invested_ratio": invested, "next_open_key_trades": trades}


def main() -> None:
    root = ROOT.resolve()
    protocol = read_json(root / "protocol.json")
    verify(protocol)
    protocol_sha = digest(root / "protocol.json")
    output = root / "execution-diagnostic"
    output.mkdir(parents=True, exist_ok=True)
    frozen = {(period, cost): baseline(root, protocol, period, cost)
              for period in PERIODS for cost in ((1, 2) if period == "late" else (1,))}
    traces = {cost: replay(root, protocol, "late", 1_000_000, cost=cost) for cost in (1, 2)}
    saved_paths = compare_saved_paths(root)
    for cost in (1, 2):
        expected = frozen["late", cost]["metrics"]
        actual = traces[cost]["metrics"]
        for key in ("final_equity", "execution_cost", "fill_count"):
            if not np.isclose(actual[key], expected[key], rtol=1e-10, atol=1e-8):
                raise ValueError(f"Baseline replay differs: cost={cost} {key}")
    if traces[1]["signal_targets"] != traces[2]["signal_targets"]:
        raise ValueError("Cost replays have different strategy targets")
    first = traces[1]["trace"]
    second = traces[2]["trace"]
    if first["signal"]["weights"] != second["signal"]["weights"]:
        raise ValueError("May 22 strategy targets differ")
    cost_path = {
        "purpose": "Post-hoc explanation of higher double-cost return; not a parameter-selection input",
        "protocol_sha256": protocol_sha, "candidate": NAME, "period": "late",
        **saved_paths,
        "trigger_date": TRACE_DATE, "next_open_date": "2026-05-25",
        "strategy_target": first["signal"]["weights"],
        "cost1": {"risk_events": first["risk_events"],
                  "approved_weights": first["signal"]["approved_weights"],
                  "orders": first["orders"],
                  "metrics": {k: frozen["late", 1]["metrics"][k] for k in
                              ("total_return", "final_equity", "execution_cost", "fill_count")}},
        "cost2": {"risk_events": second["risk_events"],
                  "approved_weights": second["signal"]["approved_weights"],
                  "orders": second["orders"],
                  "metrics": {k: frozen["late", 2]["metrics"][k] for k in
                              ("total_return", "final_equity", "execution_cost", "fill_count")}},
        "terminal_equity_cost2_minus_cost1": (frozen["late", 2]["metrics"]["final_equity"]
                                               - frozen["late", 1]["metrics"]["final_equity"]),
        "interpretation": "The 80% cap repair deferred the base-cost rotation by one week; cost stress changes holdings and cannot be read as a fixed-trade fee deduction.",
    }
    if not first["risk_events"] or first["risk_events"][0]["code"] != "EXPOSURE_REDUCTION" or second["risk_events"]:
        raise ValueError("Expected May 22 cap-boundary divergence is absent")
    cases = []
    for period in PERIODS:
        base_targets = traces[1]["signal_targets"] if period == "late" else None
        capital_targets = []
        for capital in CAPITALS:
            folder = output / f"{period}__cash{capital}__cost1"
            item = replay(root, protocol, period, capital, folder=folder)
            capital_targets.append(item["signal_targets"])
            cases.append({k: v for k, v in item.items() if k not in ("trace", "signal_targets")})
        if capital_targets[0] != capital_targets[1] or (base_targets is not None and capital_targets[0] != base_targets):
            raise ValueError(f"Strategy target signals vary with initial capital: {period}")
    verify(protocol)
    sensitivity = {
        "purpose": "Post-hoc ±1% starting-capital execution sensitivity; no retuning or reselection",
        "protocol_sha256": protocol_sha, "candidate": NAME,
        "strategy_signals_equal_across_capitals": True,
        "baseline": [{"period": period, "initial_cash": 1_000_000,
                      "metrics": frozen[period, 1]["metrics"],
                      "curve_sha256": digest(destination(root, NAME, period) / "curves.csv")}
                     for period in PERIODS],
        "cases": cases,
        "limitations": ["Current universe is retrospective; all periods were previously inspected.",
                        "Capital and cost perturbations can cross the engine's exposure-repair boundary and change trade timing."],
    }
    write_json(DOCS / "cost-path-diagnostic.json", cost_path)
    write_json(DOCS / "execution-sensitivity.json", sensitivity)
    print(json.dumps({"protocol_sha256": protocol_sha,
                      "late_terminal_cost_advantage": cost_path["terminal_equity_cost2_minus_cost1"],
                      "cases": [{"period": c["period"], "cash": c["initial_cash"],
                                 "return": c["metrics"]["total_return"],
                                 "drawdown": c["metrics"]["max_drawdown"]} for c in cases]}, indent=2))


if __name__ == "__main__":
    main()
