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
    (tmp_path / "policy" / "policy.zip").write_bytes(b"tamper")
    with pytest.raises(ValueError, match="integrity|hash"):
        backtest_policy(tmp_path / "policy", tmp_path / "data", backtest, tmp_path / "tamper", lambda event: None, lambda: False)


def test_cancelled_policy_training_never_publishes_manifest(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.learning.contracts import LearningCancelled
    spec, _ = investing_case(tmp_path / "data")
    cancelled = False
    events = []

    def emit(event):
        nonlocal cancelled
        events.append(event)
        if event.get("validation_net_log_return") is not None:
            cancelled = True

    with pytest.raises(LearningCancelled):
        train_policy(spec, tmp_path / "data", tmp_path / "policy", emit, lambda: cancelled, "a" * 64)
    assert not (tmp_path / "policy" / "manifest.json").exists()
    assert (tmp_path / "policy" / "policy.zip").exists()
    assert events[-1]["training_sampling"]["episode_count"] > 0
    assert spec["train_start"] <= events[-1]["training_sampling"]["start"] <= events[-1]["training_sampling"]["end"] <= spec["train_end"]


@pytest.mark.parametrize("episode_sessions, n_steps", [(2, 8), (20, 2)])
def test_training_saves_actual_sampling_for_each_session_without_unused_resets(tmp_path, monkeypatch, episode_sessions, n_steps):
    from quanto.investing.environment import DailyETFEnv
    from quanto.investing.training import train_policy
    from quanto.learning.artifacts import read_manifest

    spec, _ = investing_case(tmp_path / "data")
    spec.update(episode_sessions=episode_sessions, total_timesteps=2, n_steps=n_steps, batch_size=n_steps)
    resets = []
    original_reset, original_step = DailyETFEnv.reset, DailyETFEnv.step

    def trace_reset(env, *args, **kwargs):
        result = original_reset(env, *args, **kwargs)
        if env.training:
            resets.append(dict(start=result[1]["episode_start"], sampled_end=result[1]["episode_end"], end=None))
        return result

    def trace_step(env, action):
        result = original_step(env, action)
        if env.training:
            resets[-1]["end"] = result[4]["asof"]
        return result

    monkeypatch.setattr(DailyETFEnv, "reset", trace_reset)
    monkeypatch.setattr(DailyETFEnv, "step", trace_step)
    events = []
    first = train_policy(spec, tmp_path / "data", tmp_path / "first", events.append,
                         lambda: False, "a" * 64, model_id="model", evaluate_test=False)

    def check_sampling(manifest):
        used = [dict(start=row["start"], end=row["end"]) for row in resets if row["end"] is not None]
        assert manifest["training_sampling_intervals"] == used
        assert manifest["training_sampling"] == dict(start=min(row["start"] for row in used),
            end=max(row["end"] for row in used), episode_count=len(used))
        assert events[-1]["training_sampling"] == manifest["training_sampling"]
        if episode_sessions == 2:
            assert len(used) == 8 and len(resets) == 9
        else:
            assert len(used) == 1 and used[-1]["end"] < resets[-1]["sampled_end"]

    check_sampling(first)
    first_intervals = first["training_sampling_intervals"]
    resets.clear()
    events.clear()
    second = train_policy(spec, tmp_path / "data", tmp_path / "second", events.append,
                          lambda: False, "a" * 64, resume_dir=tmp_path / "first", model_id="model", evaluate_test=False)
    check_sampling(second)
    assert second["training_sampling_intervals"] != first_intervals
    assert read_manifest(tmp_path / "second", "policy")["training_sampling"] == second["training_sampling"]


def test_frozen_baselines_use_experiment_execution_and_cost_multiplier(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy
    spec, dates = investing_case(tmp_path / "data")
    train_policy(spec, tmp_path / "data", tmp_path / "policy", lambda event: None, lambda: False, "a" * 64)
    interval = dict(start=str(dates[60].date()), end=str(dates[89].date()))
    cash = backtest_policy(tmp_path / "policy", tmp_path / "data", {**interval, "baseline": "cash"},
                           tmp_path / "cash", lambda event: None, lambda: False)
    assert cash["metrics"]["total_return"] == 0.
    assert cash["metrics"]["average_cash_ratio"] == 1.
    assert cash["metrics"]["turnover"] == cash["metrics"]["execution_cost"] == 0.
    assert cash["metric_definitions"]["average_cash_ratio"]["formula"] == "mean(daily_cash / daily_equity)"
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


def test_continuation_advances_window_seed_reproducibly_without_resetting_optimizer(tmp_path, monkeypatch):
    import torch
    from stable_baselines3 import PPO
    from quanto.investing.environment import DailyETFEnv
    from quanto.investing.training import train_policy
    from quanto.learning.artifacts import read_manifest, write_manifest

    spec, _ = investing_case(tmp_path / "data")
    spec = {**spec, "episode_sessions": 10}
    windows = []
    original_reset = DailyETFEnv.reset

    def trace_reset(env, *args, **kwargs):
        result = original_reset(env, *args, **kwargs)
        if env.training:
            windows.append(str(result[1]["episode_start"]))
        return result

    monkeypatch.setattr(DailyETFEnv, "reset", trace_reset)
    first = train_policy(spec, tmp_path / "data", tmp_path / "first", lambda event: None,
                         lambda: False, "a" * 64, model_id="model", evaluate_test=False)
    assert first["action_version"] == "hold-target-quartiles-v1"
    first_windows = list(windows)
    assert first["sampling_provenance"]["effective_env_seed"] == 42
    saved = PPO.load(tmp_path / "first" / "last.zip", device="cpu")
    saved_parameters = {name: value.clone() for name, value in saved.policy.state_dict().items()}
    saved_optimizer = saved.policy.optimizer.state_dict()
    saved_steps = [state["step"].item() for state in saved_optimizer["state"].values()]
    assert saved_steps and min(saved_steps) > 0
    original_learn = PPO.learn
    entry_steps = []

    def verify_resume(model, *args, **kwargs):
        assert model.num_timesteps == first["timesteps_completed"]
        assert all(torch.equal(saved_parameters[name], value) for name, value in model.policy.state_dict().items())
        actual = model.policy.optimizer.state_dict()
        assert actual["param_groups"] == saved_optimizer["param_groups"]
        for index, state in saved_optimizer["state"].items():
            assert all(torch.equal(value, actual["state"][index][name]) for name, value in state.items())
        entry_steps.append([state["step"].item() for state in actual["state"].values()])
        return original_learn(model, *args, **kwargs)

    monkeypatch.setattr(PPO, "learn", verify_resume)
    # An existing pre-fix checkpoint must be resumable without modifying its fixed config.
    legacy = read_manifest(tmp_path / "first", "policy")
    legacy.pop("sampling_provenance", None)
    legacy["config"].pop("action_version")
    legacy.pop("id")
    write_manifest(tmp_path / "first", legacy)
    resumed = []
    for name in ("second", "retry"):
        windows.clear()
        manifest = train_policy(spec, tmp_path / "data", tmp_path / name, lambda event: None,
                                lambda: False, "a" * 64, resume_dir=tmp_path / "first",
                                model_id="model", evaluate_test=False)
        resumed.append((list(windows), manifest))
    assert resumed[0][0] != first_windows
    assert resumed[0][0] == resumed[1][0]
    assert resumed[0][1]["history"] == resumed[1][1]["history"]
    assert entry_steps == [saved_steps, saved_steps]
    assert resumed[0][1]["timesteps_started"] == 16
    assert resumed[0][1]["timesteps_completed"] == 32
    assert resumed[0][1]["sampling_provenance"] == dict(
        algorithm="base-seed-plus-parent-timesteps-v1", base_seed=42,
        parent_timesteps=16, effective_env_seed=58)
    assert resumed[0][1]["config"] == first["config"]
    read_manifest(tmp_path / "second", "policy")
    second = PPO.load(tmp_path / "second" / "last.zip", device="cpu")
    retry = PPO.load(tmp_path / "retry" / "last.zip", device="cpu")
    assert all(torch.equal(value, retry.policy.state_dict()[name]) for name, value in second.policy.state_dict().items())
    assert any(not torch.equal(value, saved_parameters[name]) for name, value in second.policy.state_dict().items())
    assert all(state["step"].item() > saved_steps[0] for state in second.policy.optimizer.state_dict()["state"].values())

    # A new-format parent follows the same rule on the next continuation.
    first = resumed[0][1]
    saved_parameters = {name: value.clone() for name, value in second.policy.state_dict().items()}
    saved_optimizer = second.policy.optimizer.state_dict()
    windows.clear()
    third = train_policy(spec, tmp_path / "data", tmp_path / "third", lambda event: None,
                         lambda: False, "a" * 64, resume_dir=tmp_path / "second",
                         model_id="model", evaluate_test=False)
    assert windows != resumed[0][0]
    assert third["sampling_provenance"]["effective_env_seed"] == 74
    assert third["timesteps_completed"] == 48
