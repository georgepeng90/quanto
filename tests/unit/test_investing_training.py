import pandas as pd
import pytest

from tests.fixtures.investing_training_cases import investing_case


def test_policy_contract_rejects_shared_dates_and_outside_coverage(tmp_path):
    from quanto.investing.contracts import PolicyTrainingSpec, PolicyBacktestSpec
    spec, _ = investing_case(tmp_path / "data")
    config = PolicyTrainingSpec.model_validate(spec)
    assert config.train_end < config.validation_start
    for changed in ({"validation_start": spec["train_end"]}, {"test_end": "2025-01-01"},
                    {"batch_size": 16}, {"resume_job_id": "legacy"}):
        with pytest.raises(ValueError):
            PolicyTrainingSpec.model_validate({**spec, **changed})
    with pytest.raises(ValueError):
        PolicyBacktestSpec.model_validate(dict(start="2024-05-01", end="2024-04-01"))


def test_real_ppo_updates_selects_after_training_and_freezes_reload(tmp_path, monkeypatch):
    import torch
    from stable_baselines3 import PPO
    from stable_baselines3.common.env_checker import check_env
    from quanto.backtest.dataset_loaders import load_dataset
    from quanto.config import BacktestSpec
    from quanto.investing.environment import DailyETFEnv
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy
    from quanto.learning.artifacts import read_manifest
    from quanto.objects import file_hash
    spec, dates = investing_case(tmp_path / "data")
    env = DailyETFEnv(load_dataset(tmp_path / "data"), BacktestSpec.model_validate(spec["backtest"]), lookback=5)
    check_env(env)
    original_learn = PPO.learn
    parameter_updates = []

    def measured_learn(model, *args, **kwargs):
        before = [value.detach().clone() for value in model.policy.parameters()]
        answer = original_learn(model, *args, **kwargs)
        parameter_updates.append(any(not torch.equal(first, last) for first, last in zip(before, model.policy.parameters())))
        return answer

    monkeypatch.setattr(PPO, "learn", measured_learn)
    import quanto.investing.training as training
    original_evaluate = training.evaluate_policy
    evaluations = []

    def measured_evaluate(model, bundle, backtest, *args, **kwargs):
        evaluations.append((backtest.start, model._n_updates))
        return original_evaluate(model, bundle, backtest, *args, **kwargs)

    monkeypatch.setattr(training, "evaluate_policy", measured_evaluate)
    manifest = train_policy(spec, tmp_path / "data", tmp_path / "policy", lambda event: None, lambda: False, "a" * 64)
    assert parameter_updates == [True]
    assert manifest["timesteps_completed"] >= 16
    assert evaluations and all(updates > 0 for _, updates in evaluations)
    assert sum(start == dates[60].date() for start, _ in evaluations) == 1
    best_score = max(row["validation_net_log_return"] for row in manifest["history"])
    assert manifest["metrics"]["validation"]["net_log_return"] == best_score
    assert manifest["best_timesteps"] == next(row["timesteps"] for row in manifest["history"]
                                               if row["validation_net_log_return"] == best_score)
    assert read_manifest(tmp_path / "policy", "policy")["id"] == manifest["id"]
    model_hash = file_hash(tmp_path / "policy" / "policy.zip")
    backtest = dict(start=str(dates[60].date()), end=str(dates[89].date()), baseline="policy")
    first = backtest_policy(tmp_path / "policy", tmp_path / "data", backtest, tmp_path / "first", lambda event: None, lambda: False)
    second = backtest_policy(tmp_path / "policy", tmp_path / "data", backtest, tmp_path / "second", lambda event: None, lambda: False)
    assert first["metrics"] == second["metrics"]
    pd.testing.assert_frame_equal(pd.read_csv(tmp_path / "first" / "equity.csv"), pd.read_csv(tmp_path / "second" / "equity.csv"))
    assert file_hash(tmp_path / "policy" / "policy.zip") == model_hash
    assert parameter_updates == [True]
    with pytest.raises(ValueError, match="cutoff|validation"):
        backtest_policy(tmp_path / "policy", tmp_path / "data", {**backtest, "start": spec["validation_end"]}, tmp_path / "bad", lambda event: None, lambda: False)
    (tmp_path / "policy" / "policy.zip").write_bytes(b"tamper")
    with pytest.raises(ValueError, match="integrity|hash"):
        backtest_policy(tmp_path / "policy", tmp_path / "data", backtest, tmp_path / "tamper", lambda event: None, lambda: False)


def test_cancelled_policy_training_never_publishes_manifest(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.learning.contracts import LearningCancelled
    spec, _ = investing_case(tmp_path / "data")
    cancelled = False

    def emit(event):
        nonlocal cancelled
        if event.get("validation_net_log_return") is not None:
            cancelled = True

    with pytest.raises(LearningCancelled):
        train_policy(spec, tmp_path / "data", tmp_path / "policy", emit, lambda: cancelled, "a" * 64)
    assert not (tmp_path / "policy" / "manifest.json").exists()
    assert (tmp_path / "policy" / "policy.zip").exists()


def test_frozen_baselines_use_experiment_execution_and_cost_multiplier(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy
    spec, dates = investing_case(tmp_path / "data")
    train_policy(spec, tmp_path / "data", tmp_path / "policy", lambda event: None, lambda: False, "a" * 64)
    interval = dict(start=str(dates[60].date()), end=str(dates[89].date()))
    cash = backtest_policy(tmp_path / "policy", tmp_path / "data", {**interval, "baseline": "cash"},
                           tmp_path / "cash", lambda event: None, lambda: False)
    assert cash["metrics"]["total_return"] == 0.
    assert cash["counts"]["fills"] == 0
    ordinary = backtest_policy(tmp_path / "policy", tmp_path / "data", {**interval, "baseline": "buy_hold"},
                               tmp_path / "ordinary", lambda event: None, lambda: False)
    stressed = backtest_policy(tmp_path / "policy", tmp_path / "data", {**interval, "baseline": "buy_hold", "cost_multiplier": 2.},
                               tmp_path / "stressed", lambda event: None, lambda: False)
    assert ordinary["counts"]["fills"] > 0
    assert stressed["assumptions"]["execution"]["commission_rate"] == .0006
    assert stressed["metrics"]["execution_cost"] > ordinary["metrics"]["execution_cost"]
    assert stressed["metrics"]["final_equity"] < ordinary["metrics"]["final_equity"]


def test_training_rejects_splits_without_history(tmp_path):
    from quanto.investing.training import train_policy
    spec, _ = investing_case(tmp_path / "data")
    with pytest.raises(ValueError, match="history|lookback"):
        train_policy({**spec, "lookback": 120}, tmp_path / "data", tmp_path / "policy", lambda event: None, lambda: False, "a" * 64)


def test_changed_test_prices_cannot_change_training_or_checkpoint_selection(tmp_path):
    import numpy as np
    import torch
    from stable_baselines3 import PPO
    from quanto.investing.training import train_policy

    spec, dates = investing_case(tmp_path / "original-data")
    investing_case(tmp_path / "changed-data")
    changed = pd.read_csv(tmp_path / "changed-data" / "prices.csv", dtype={"symbol": str})
    test_mask = pd.to_datetime(changed.date) >= dates[60]
    factors = np.linspace(1., 2., test_mask.sum())
    for column in ("open", "high", "low", "close"):
        changed.loc[test_mask, column] *= factors
    changed.to_csv(tmp_path / "changed-data" / "prices.csv", index=False)
    first = train_policy(spec, tmp_path / "original-data", tmp_path / "original-policy", lambda event: None, lambda: False, "a" * 64)
    second = train_policy(spec, tmp_path / "changed-data", tmp_path / "changed-policy", lambda event: None, lambda: False, "b" * 64)
    assert first["history"] == second["history"]
    assert first["metrics"]["validation"] == second["metrics"]["validation"]
    assert first["best_timesteps"] == second["best_timesteps"]
    first_state = PPO.load(tmp_path / "original-policy" / "policy.zip", device="cpu").policy.state_dict()
    second_state = PPO.load(tmp_path / "changed-policy" / "policy.zip", device="cpu").policy.state_dict()
    assert all(torch.equal(first_state[name], second_state[name]) for name in first_state)
