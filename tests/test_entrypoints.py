"""Smoke tests for the minimal runnable entry points."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_script(script_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "src" / script_name)],
        check=True,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
    )


def test_data_fetcher_entrypoint_runs() -> None:
    result = run_script("data_fetcher.py")
    assert "当前阶段：数据拉取准备" in result.stdout


def test_multi_factor_strategy_entrypoint_runs() -> None:
    result = run_script("multi_factor_strategy.py")
    assert "当前阶段：传统多因子回测准备" in result.stdout


def test_gru_model_entrypoint_runs() -> None:
    result = run_script("gru_model.py")
    assert "当前阶段：GRU 模型训练准备" in result.stdout
