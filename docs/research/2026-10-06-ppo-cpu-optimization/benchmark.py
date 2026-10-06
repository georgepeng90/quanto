import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from quanto.backtest.dataset_loaders import load_dataset
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.environment import DailyETFEnv
from quanto.investing.training import split_spec


def encoded(value):
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()


def measure(bundle, config, collect, episodes):
    env = DailyETFEnv(bundle, split_spec(config, "train"), frequency=config.frequency,
        lookback=config.lookback, episode_sessions=config.episode_sessions, training=True,
        action_version=config.action_version, reward_version=config.reward_version, collect=collect)
    trace = hashlib.sha256()
    reports = hashlib.sha256()
    reset_seconds, starts, decisions = [], [], 0
    started = time.perf_counter()
    for episode in range(episodes):
        before = time.perf_counter()
        observation, info = env.reset(seed=42 if episode == 0 else None)
        reset_seconds.append(time.perf_counter() - before)
        starts.append(info["episode_start"])
        trace.update(encoded([observation.tolist(), info]))
        done, step = False, 0
        while not done:
            # Deterministic hold, target, reduction and exit paths; no model or GPU.
            action = np.zeros(len(config.backtest.symbols) + 1, dtype=np.int64)
            if step % 4 != 1:
                action[0] = 1
                action[1:] = (np.arange(len(config.backtest.symbols)) + step) % 5
            observation, reward, terminated, truncated, info = env.step(action)
            trace.update(encoded([observation.tolist(), reward, terminated, truncated, info]))
            done = terminated or truncated
            step += 1
            decisions += 1
        if collect:
            result = env.result()
            for name in ("equity", "orders", "fills", "positions", "signals", "account_events", "risk_events"):
                reports.update(encoded([name, getattr(result, name).to_dict("records")]))
    elapsed = time.perf_counter() - started
    trace.update(encoded(env.reward_diagnostics))
    env.close()
    return dict(reward=config.reward_version, collect=collect, episodes=episodes,
        decision_steps=decisions, elapsed_seconds=elapsed, reset_seconds=reset_seconds,
        episode_starts=starts, trace_sha256=trace.hexdigest(), tables_sha256=reports.hexdigest())


parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
trial = json.loads(Path("docs/research/2026-10-06-32etf-dsr/trial/trial.json").read_text(encoding="utf-8"))
bundle = load_dataset(Path(trial["dataset_path"]))
cases = []
for role in ("control", "dsr"):
    config = PolicyTrainingSpec.model_validate(trial["specs"][role])
    for collect, episodes in ((False, 8), (True, 2)):
        case = measure(bundle, config, collect, episodes)
        case["role"] = role
        cases.append(case)
        print(json.dumps(case, ensure_ascii=False), flush=True)
args.output.write_text(json.dumps(dict(price_rows=len(bundle.prices), cases=cases), indent=2), encoding="utf-8")
