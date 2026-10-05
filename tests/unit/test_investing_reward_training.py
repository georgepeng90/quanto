import json
import math

import pandas as pd
import pytest

from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from tests.fixtures.investing_training_cases import investing_case


def data_hash(path):
    return digest(json_bytes(hash_files(path)))


@pytest.mark.parametrize("reward", ["net-log-return-with-state-v1", "differential-sharpe-v1"])
def test_state_reward_trains_and_continues_without_a_test_partition(tmp_path, reward):
    from stable_baselines3 import PPO
    from quanto.investing.training import train_policy

    spec, _ = investing_case(tmp_path / "data")
    spec.update(reward_version=reward, test_start=None, test_end=None)
    spec["backtest"]["end"] = spec["validation_end"]
    first = train_policy(spec, tmp_path / "data", tmp_path / "first", lambda _: None,
                         lambda: False, data_hash(tmp_path / "data"), model_id="new-model")
    assert first["reward_version"] == reward
    assert first["reward_parameters"]["eta"] == .01
    assert first["metrics"]["test"] is None
    assert not any("test evaluated once" in text for text in first["research_limits"])
    assert first["training_diagnostics"]["reward_daily_count"] > 0
    assert math.isfinite(first["training_diagnostics"]["training_reward_sum"])
    model = PPO.load(tmp_path / "first" / "last.zip", device="cpu")
    assert model.observation_space.shape == (18,)
    second = train_policy({**spec, "total_timesteps": 8}, tmp_path / "data", tmp_path / "second",
                          lambda _: None, lambda: False, data_hash(tmp_path / "data"),
                          model_id="new-model", resume_dir=tmp_path / "first")
    assert second["timesteps_started"] == first["timesteps_completed"]
    assert second["timesteps_completed"] == first["timesteps_completed"] + 8
    assert second["reward_version"] == reward
    with pytest.raises(ValueError, match="configuration|identity"):
        train_policy({**spec, "reward_version": "net-log-return-v1"}, tmp_path / "data", tmp_path / "bad",
                     lambda _: None, lambda: False, data_hash(tmp_path / "data"),
                     model_id="new-model", resume_dir=tmp_path / "first")


def test_dsr_evaluation_reconciles_cash_prefix_independently_of_reward(tmp_path):
    from quanto.backtest.dataset_loaders import load_dataset
    from quanto.config import BacktestSpec
    from quanto.investing.evaluation import evaluate_policy

    spec, _ = investing_case(tmp_path / "data")
    spec["backtest"]["metrics"]["cash_rate"] = .12
    metrics, result, _ = evaluate_policy(None, load_dataset(tmp_path / "data"),
        BacktestSpec.model_validate(spec["backtest"]), lookback=5, baseline="cash",
        reward_version="differential-sharpe-v1")
    expected = math.log(result.equity.iloc[-1].equity / spec["backtest"]["initial_cash"])
    assert metrics["prefix_net_log_return"] > 0
    assert metrics["net_log_return"] == pytest.approx(expected)
    assert metrics["action_net_log_return"] + metrics["prefix_net_log_return"] == pytest.approx(expected)
    assert metrics["evaluation_reward_sum"] != pytest.approx(expected)
    assert metrics["reward_daily_count"] == len(result.equity)


def test_legacy_manifest_without_reward_fields_loads_and_continues_unchanged(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy
    from quanto.learning.artifacts import write_manifest

    spec, dates = investing_case(tmp_path / "data")
    manifest = train_policy(spec, tmp_path / "data", tmp_path / "old", lambda _: None,
                            lambda: False, data_hash(tmp_path / "data"), model_id="legacy", evaluate_test=False)
    manifest.pop("id")
    manifest.pop("reward_version", None)
    manifest.pop("reward_parameters", None)
    manifest["config"].pop("reward_version", None)
    write_manifest(tmp_path / "old", manifest)
    old_files = hash_files(tmp_path / "old")
    summary = backtest_policy(tmp_path / "old", tmp_path / "data",
        dict(start=str(dates[60].date()), end=str(dates[89].date()), baseline="cash"),
        tmp_path / "backtest", lambda _: None, lambda: False)
    assert summary["metadata"]["reward_version"] == "net-log-return-v1"
    resumed = train_policy(spec, tmp_path / "data", tmp_path / "new", lambda _: None,
                           lambda: False, data_hash(tmp_path / "data"), model_id="legacy",
                           resume_dir=tmp_path / "old", evaluate_test=False)
    assert resumed["timesteps_started"] == manifest["timesteps_completed"]
    assert hash_files(tmp_path / "old") == old_files


def test_validation_report_uses_selected_policy_and_checks_dataset(tmp_path, monkeypatch):
    from stable_baselines3 import PPO
    from quanto.investing.training import train_policy
    from quanto.investing import evaluation

    spec, _ = investing_case(tmp_path / "data")
    manifest = train_policy(spec, tmp_path / "data", tmp_path / "policy", lambda _: None,
                            lambda: False, data_hash(tmp_path / "data"), evaluate_test=False)
    loaded = []
    original_load = PPO.load

    def load(path, **kwargs):
        loaded.append(path.name)
        return original_load(path, **kwargs)

    monkeypatch.setattr(PPO, "load", load)
    summary = evaluation.evaluate_validation_policy(tmp_path / "policy", tmp_path / "data",
        tmp_path / "report", lambda _: None, lambda: False)
    assert loaded == ["policy.zip"]
    assert summary["kind"] == "policy_validation"
    assert summary["evaluation_purpose"] == "model_selection"
    assert summary["period"] == dict(start=spec["validation_start"], end=spec["validation_end"])
    assert summary["policy_content_id"] == manifest["id"]
    for name in ("equity", "orders", "fills", "positions", "signals"):
        assert (tmp_path / "report" / f"{name}.csv").is_file()
    assert json.loads((tmp_path / "report" / "summary.json").read_text(encoding="utf-8"))["kind"] == "policy_validation"
    prices = pd.read_csv(tmp_path / "data" / "prices.csv", dtype={"symbol": str})
    prices.loc[0, "amount"] += 1
    prices.to_csv(tmp_path / "data" / "prices.csv", index=False)
    with pytest.raises(ValueError, match="dataset.*hash|dataset.*identity"):
        evaluation.evaluate_validation_policy(tmp_path / "policy", tmp_path / "data",
            tmp_path / "wrong", lambda _: None, lambda: False)
    assert not (tmp_path / "wrong").exists()
