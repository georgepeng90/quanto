"""Deterministic, pre-framework ETF behavior. Never refresh to bless a regression."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd

from quanto.backtest.models import TABLE_COLUMNS
from quanto.investing.contracts import CASH_ACTION_VERSION, LEGACY_ACTION_VERSION
from quanto.investing.environment import DailyETFEnv, environment_version, feature_version, EXECUTION_VERSION
from quanto.investing.rewards import LEGACY_REWARD_VERSION, STATE_REWARD_VERSIONS, reward_parameters
from quanto.learning.registry import runtime_versions
from tests.fixtures.engine_cases import bundle_case, spec_case


REFERENCE_PATH = Path(__file__).with_suffix(".json")
SYMBOLS = ("510300", "159915")
REWARDS = (LEGACY_REWARD_VERSION, *sorted(STATE_REWARD_VERSIONS))


def _value(value):
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, np.ndarray)):
        return [_value(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    return value


def _table(frame):
    return {"columns": frame.columns.tolist(), "rows": [_value(row) for row in frame.itertuples(index=False, name=None)]}


def reference_bundle(price_mode="raw_cash", *, monthly=False, risk=False):
    """Two explicitly identified synthetic ETFs; dates are a synthetic weekday calendar."""
    original = bundle_case(symbols=SYMBOLS)
    start = "2023-09-25" if monthly else "2024-01-01"
    end = "2024-03-01" if monthly else "2024-02-05"
    dates = pd.bdate_range(start, end)
    calendar = pd.bdate_range(start, "2024-03-08" if monthly else "2024-02-12")
    rows = []
    for index, day in enumerate(dates):
        for offset, symbol in enumerate(SYMBOLS):
            close = 10. + offset * 5 + index * .03 + (index % 3 - 1) * .08
            if risk and day >= pd.Timestamp("2024-01-09"):
                close *= .45
            rows.append(dict(date=day, symbol=symbol, open=close * .997,
                             high=close * 1.01, low=close * .99, close=close,
                             volume=100000 + index * 100, amount=1000000 + index * 1000))
    prices = pd.DataFrame(rows)
    events = pd.DataFrame([dict(symbol=SYMBOLS[0], event_type="dividend",
                               record_date=pd.Timestamp("2024-01-08"), ex_date=pd.Timestamp("2024-01-09"),
                               pay_date=pd.Timestamp("2024-01-10"), cash_per_share=.1, split_ratio=None)])
    manifest = original.manifest.model_copy(update=dict(
        source="explicit synthetic ETF baseline; weekday calendar is not an exchange calendar",
        price_mode=price_mode, calendar_start=calendar[0].date(), calendar_end=calendar[-1].date(),
        data_start=dates[0].date(), data_end=dates[-1].date(),
        limitations=["Synthetic adjusted research quotes; cash events are not replayed."]
        if price_mode == "adjusted_research" else []))
    return replace(original, manifest=manifest, prices=prices,
                   benchmark=pd.DataFrame(dict(date=dates, close=100 + np.arange(len(dates)), turnover=10000000)),
                   calendar=pd.DataFrame(dict(date=calendar)),
                   instruments=original.instruments.assign(listing_date=dates[0]),
                   status=prices[["date", "symbol"]].assign(status="trading", can_buy=True, can_sell=True),
                   events=events, eligibility=prices[["date", "symbol"]].assign(eligible=True))


def case_definitions():
    cases = []
    for action in (LEGACY_ACTION_VERSION, CASH_ACTION_VERSION):
        for reward in REWARDS:
            cases.append(dict(id=f"weekly-{action}-{reward}", dataset="weekly-raw", action=action,
                              reward=reward, frequency="weekly", lookback=5, risk=False))
    cases.extend([
        dict(id="monthly-legacy", dataset="monthly-adjusted", action=LEGACY_ACTION_VERSION,
             reward=LEGACY_REWARD_VERSION, frequency="monthly", lookback=61, risk=False),
        dict(id="monthly-dsr", dataset="monthly-adjusted", action=CASH_ACTION_VERSION,
             reward="differential-sharpe-v1", frequency="monthly", lookback=61, risk=False),
        dict(id="weekly-adjusted", dataset="weekly-adjusted", action=CASH_ACTION_VERSION,
             reward=LEGACY_REWARD_VERSION, frequency="weekly", lookback=5, risk=False),
        dict(id="daily-risk", dataset="weekly-risk", action=CASH_ACTION_VERSION,
             reward="differential-sharpe-v1", frequency="weekly", lookback=5, risk=True),
    ])
    return cases


def _capture_case(case, bundle):
    spec = spec_case(bundle, start="2024-01-01", initial_cash=10000,
                     execution={"commission_rate": .0003, "minimum_commission": .1, "slippage_rate": .0005},
                     metrics={"cash_rate": .0252},
                     risk={"drawdown_pause": .2, "pause_action": "liquidate"} if case["risk"] else {})
    env = DailyETFEnv(bundle, spec, frequency=case["frequency"], lookback=case["lookback"],
                      action_version=case["action"], reward_version=case["reward"], collect=True)
    try:
        observation, info = env.reset(seed=17)
        initial = dict(observation=observation.tolist(), info=info)
        trace = []
        for index in range(len(env.decision_dates)):
            if index == 0:
                action = [1, 4, 4]
            elif index == 2:
                action = [2, 4, 1] if case["action"] == CASH_ACTION_VERSION else [1, 0, 0]
            elif index == 3:
                action = [1, 2, 1]
            else:
                action = [0, 4, 4]
            observation, reward, terminal, truncated, info = env.step(np.array(action, dtype=np.int64))
            trace.append(dict(action=action, observation=observation.tolist(), reward=reward,
                              terminal=terminal, truncated=truncated, info=info))
            if terminal or truncated:
                break
        result = env.result()
        return _value(dict(input=case, backtest_spec=spec.model_dump(mode="json"),
                           identity=dict(feature=feature_version(case["lookback"], case["reward"]),
                                         environment=environment_version(case["reward"]), execution=EXECUTION_VERSION,
                                         reward_parameters=reward_parameters(case["reward"])),
                           action_space=env.action_space.nvec, initial=initial, trace=trace,
                           reward_diagnostics=env.reward_diagnostics,
                           tables={name: _table(getattr(result, name)[columns]) for name, columns in TABLE_COLUMNS.items()},
                           completed_through=result.completed_through))
    finally:
        env.close()


def capture_etf_reference() -> dict:
    bundles = {"weekly-raw": reference_bundle(),
               "weekly-adjusted": reference_bundle("adjusted_research"),
               "weekly-risk": reference_bundle(risk=True),
               "monthly-adjusted": reference_bundle("adjusted_research", monthly=True)}
    inputs = {}
    for key, bundle in bundles.items():
        inputs[key] = dict(manifest=bundle.manifest.model_dump(mode="json"),
                           identity_evidence={"510300": "SH:etf:510300", "159915": "SZ:etf:159915"},
                           evidence_kind="synthetic_fixture_not_provider_evidence",
                           tables={name: _table(getattr(bundle, name)) for name in
                                   ("prices", "benchmark", "calendar", "instruments", "status", "events", "eligibility")})
    return dict(inputs=inputs, cases={case["id"]: _capture_case(case, bundles[case["dataset"]])
                                     for case in case_definitions()})


def _formatted(value, depth=0):
    """Keep table rows/observations on one line so the frozen evidence is reviewable."""
    indent = "  " * depth
    if isinstance(value, dict):
        entries = ["  " * (depth + 1) + json.dumps(key) + ": " + _formatted(item, depth + 1)
                   for key, item in value.items()]
        return "{\n" + ",\n".join(entries) + "\n" + indent + "}"
    if isinstance(value, list) and value and isinstance(value[0], (list, dict)):
        return "[\n" + ",\n".join("  " * (depth + 1) + json.dumps(item, ensure_ascii=False, allow_nan=False)
                                     for item in value) + "\n" + indent + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def write_reference(path: Path = REFERENCE_PATH) -> None:
    if path.exists():
        raise FileExistsError("Frozen ETF reference already exists; do not regenerate expected behavior")
    root = Path(__file__).resolve().parents[2]
    source_paths = sorted((root / "src/quanto").rglob("*.py")) + [Path(__file__), root / "tests/fixtures/engine_cases.py"]
    source_hashes = {item.relative_to(root).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest() for item in source_paths}
    artifact = dict(schema_version=1,
                    evidence=dict(source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
                                  source_hashes=source_hashes, dependencies=runtime_versions(),
                                  purpose="Fixed-input behavior before framework integration; not investment performance evidence"),
                    **capture_etf_reference())
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_formatted(artifact) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-reference", action="store_true", required=True)
    parser.parse_args()
    write_reference()
    print(f"Wrote {REFERENCE_PATH}")
