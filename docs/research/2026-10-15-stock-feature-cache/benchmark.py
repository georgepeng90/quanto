"""Real 34-stock episode benchmark; run each source root in a fresh process.

Example: .venv/Scripts/python.exe docs/research/2026-10-15-stock-feature-cache/benchmark.py
  --source-root data/research-20261001/job-inputs/8f421bb4-42ad-479b-894c-b80b363fcec5
  --output outputs/stock-feature-cache-20261015/baseline-state-log.json --collect
Compare trace, feature, table, diagnostic and identity hashes, never timing fields.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


PROJECT = Path(__file__).resolve().parents[3]
FROZEN = PROJECT / "data/research-20261001/job-inputs/8f421bb4-42ad-479b-894c-b80b363fcec5"


def encoded(value):
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def episode_coverage(calendar_days, start, end, seen):
    """Count dates already visited in earlier episodes of this environment."""
    days = {day for day in calendar_days if start <= day <= end}
    coverage = dict(sessions=len(days), first_visit_sessions=len(days - seen),
                    repeated_sessions=len(days & seen))
    seen.update(days)
    return coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--episodes", type=int, default=3)
    parser.add_argument("--reward", choices=("state-log", "dsr"), default="state-log")
    parser.add_argument("--collect", action="store_true")
    args = parser.parse_args()
    if args.episodes < 1:
        parser.error("--episodes must be positive")
    source = args.source_root.resolve()
    sys.path.insert(0, str(source / "src"))
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[name] = "1"

    import numpy as np
    from quanto.backtest.dataset_loaders import load_dataset
    from quanto.backtest.stock_trading import STOCK_TRADING_DEFINITION
    from quanto.framework import AssetBinding
    from quanto.investing.contracts import PolicyTrainingSpec
    from quanto.investing.feature_runtime import LearningRuntime
    from quanto.investing.stock_definitions import (
        STOCK_PPO_STATE_LOG_DEFINITION, STOCK_PPO_DSR_DEFINITION,
    )
    from quanto.investing.training import split_spec

    # The descriptor is read only; never invoke a worker, resume or training job.
    payload = json.loads((FROZEN / "input.json").read_text(encoding="utf-8"))["input"]
    definition = STOCK_PPO_STATE_LOG_DEFINITION if args.reward == "state-log" else STOCK_PPO_DSR_DEFINITION
    spec = dict(payload["request"]["spec"])
    spec["reward_version"] = definition.reward_version
    config = PolicyTrainingSpec.model_validate(spec)
    binding = AssetBinding.model_validate(payload["resolved_configuration"]["asset_binding"])
    runtime = LearningRuntime(definition, binding, STOCK_TRADING_DEFINITION)
    dataset_path = Path(payload["dataset_ref"]["path"])
    dataset_hash = payload["dataset_ref"]["content_hash"]
    del payload
    bundle = load_dataset(dataset_path)
    runtime.validate_bundle(bundle, config, dataset_hash)
    assert len(config.backtest.symbols) == 34
    assert config.episode_sessions == 126
    assert definition.feature_definition.fit is None
    env = runtime.create_environment(bundle, config, None, backtest=split_spec(config, "train"),
        training=True, cancelled=lambda: False, collect=args.collect)
    identity = runtime.identity(config)
    episodes = []
    calendar_days = tuple(day.date().isoformat() for day in bundle.calendar.date)
    seen = set()
    started = time.perf_counter()
    try:
        for episode in range(args.episodes):
            before = time.perf_counter()
            observation, info = env.reset(seed=42 if episode == 0 else None)
            reset_seconds = time.perf_counter() - before
            trace, features = hashlib.sha256(), hashlib.sha256()
            trace.update(encoded([observation.tolist(), info]))
            features.update(encoded([str(env.context.asof), env.context.features.frame_hash]))
            step, done = 0, False
            while not done:
                # Hold, target 4 stocks, reduce to 2 stocks, then explicit cash.
                action = np.zeros(len(config.backtest.symbols) + 1, dtype=np.int64)
                mode = step % 4
                if mode in (1, 2):
                    action[0] = 1
                    action[1:5 if mode == 1 else 3] = 1
                elif mode == 3:
                    action[0] = 2
                observation, reward, terminated, truncated, info = env.step(action)
                trace.update(encoded([action.tolist(), observation.tolist(), reward, terminated, truncated, info]))
                features.update(encoded([str(env.context.asof), env.context.features.frame_hash]))
                step += 1
                done = terminated or truncated
            elapsed = time.perf_counter() - before
            tables = {}
            if args.collect:
                result = env.result()
                for name in ("equity", "orders", "fills", "positions", "signals", "account_events", "risk_events"):
                    frame = getattr(result, name)
                    tables[name] = dict(rows=len(frame), sha256=hashlib.sha256(encoded(frame.to_dict("records"))).hexdigest())
                assert len(result.equity) == 126
            report = dict(episode=episode + 1, elapsed_seconds=elapsed, reset_seconds=reset_seconds,
                decision_steps=step, episode_start=info["episode_start"], episode_end=info["episode_end"],
                completed_through=info["asof"], trace_sha256=trace.hexdigest(),
                decision_features_sha256=features.hexdigest(), tables=tables,
                reward_diagnostics=dict(env.reward_diagnostics),
                date_coverage=episode_coverage(calendar_days, info["episode_start"], info["episode_end"], seen))
            episodes.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        env.close()
    output = dict(source_root=str(source), reward=args.reward, collect=args.collect,
        seed=42, symbols=len(config.backtest.symbols), episode_sessions=config.episode_sessions,
        dataset_path=str(dataset_path), price_rows=len(bundle.prices), identity=identity,
        elapsed_seconds=time.perf_counter() - started, episodes=episodes,
        source_files_sha256={name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("src/quanto/data/point_in_time.py", "src/quanto/factors/features.py")})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
