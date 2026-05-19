from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    out = df.copy()
    for col in out.columns:
        if "date" in col:
            out[col] = pd.to_datetime(out[col]).dt.strftime("%Y-%m-%d")
    out.to_csv(path, index=False)


def write_backtest_outputs(
    output_dir: str | Path,
    run_id: str,
    equity_curve: pd.DataFrame,
    trades: pd.DataFrame,
    positions: pd.DataFrame,
    signals: pd.DataFrame,
    metrics: dict,
) -> Path:
    run_dir = Path(output_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(equity_curve, run_dir / "equity_curve.csv")
    _write_csv(trades, run_dir / "trades.csv")
    _write_csv(positions, run_dir / "positions.csv")
    _write_csv(signals, run_dir / "signals.csv")
    with (run_dir / "metrics.json").open("w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    with (run_dir / "report.md").open("w", encoding="utf-8") as f:
        f.write("# Backtest Report\n\n")
        f.write(f"- Final equity: {metrics['final_equity']:.2f}\n")
        f.write(f"- Total return: {metrics['total_return']:.2%}\n")
        f.write(f"- Annual return: {metrics['annual_return']:.2%}\n")
        f.write(f"- Max drawdown: {metrics['max_drawdown']:.2%}\n")
        f.write(f"- Sharpe: {metrics['sharpe']:.3f}\n")
        f.write(f"- Calmar: {metrics['calmar']:.3f}\n")
        f.write(f"- Trades: {metrics['trade_count']}\n")
    return run_dir
