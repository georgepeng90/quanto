import json

import pytest
import torch
from stable_baselines3 import PPO

from quanto.backtest.dataset_loaders import load_dataset
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.environment import DailyETFEnv
from quanto.investing.training import split_spec, train_policy
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from tests.fixtures.investing_training_cases import investing_case


@pytest.fixture
def source_case(tmp_path):
    dataset = tmp_path / "data"
    spec, _ = investing_case(dataset)
    spec.update(total_timesteps=8, selection_metric="validation_sharpe")
    source = tmp_path / "source"
    manifest = train_policy(spec, dataset, source, lambda event: None, lambda: False,
                            digest(json_bytes(hash_files(dataset))), evaluate_test=False)
    return source, dataset, manifest


def test_learning_rate_changes_schedule_and_optimizer_without_resetting_moments(source_case):
    # Catches stale SB3 schedules and reconstructing the optimizer instead of restoring it.
    from scripts.run_policy_lr_trial import load_trial_model

    source, dataset, manifest = source_case
    config = PolicyTrainingSpec.model_validate(manifest["config"])
    original = PPO.load(source / "last.zip", device="cpu")
    env = DailyETFEnv(load_dataset(dataset), split_spec(config, "train"), lookback=config.lookback,
                      episode_sessions=config.episode_sessions, training=True, collect=False)
    try:
        model = load_trial_model(source / "last.zip", env, .00003, "cpu")
        assert model.learning_rate == .00003
        assert model.lr_schedule(.5) == .00003
        assert model.num_timesteps == 8
        for name, value in original.policy.state_dict().items():
            assert torch.equal(value, model.policy.state_dict()[name])
        previous = original.policy.optimizer.state_dict()["state"]
        restored = model.policy.optimizer.state_dict()["state"]
        for key, values in previous.items():
            for name, value in values.items():
                assert torch.equal(value, restored[key][name])
        assert all(group["lr"] == .00003 for group in model.policy.optimizer.param_groups)
        model.learn(total_timesteps=8, reset_num_timesteps=False)
        assert all(group["lr"] == .00003 for group in model.policy.optimizer.param_groups)
        assert any(not torch.equal(value, model.policy.state_dict()[name])
                   for name, value in original.policy.state_dict().items())
        assert max(float(state["step"]) for state in model.policy.optimizer.state.values()) == 2
    finally:
        env.close()


def test_trial_records_exact_sessions_and_sampling_without_changing_source(source_case, tmp_path):
    # Catches rollover budget inflation, fixed sampler seeds, missing cash/no-trade evidence.
    from scripts.run_policy_lr_trial import run_trial

    source, dataset, manifest = source_case
    before = hash_files(source)
    output = tmp_path / "trial"
    result = run_trial(source, dataset, output, .00003, sessions=2, timesteps=8, device="cpu")
    assert hash_files(source) == before
    assert result["timesteps_started"] == 8
    assert result["timesteps_completed"] == 24
    assert result["session_count"] == 2
    assert result["source_content_id"] == manifest["id"]
    assert result["best_metrics"]["no_trade"] == (result["best_metrics"]["fill_count"] == 0)
    for number, start, seed, optimizer_step in [(1, 8, 50, 1), (2, 16, 58, 2)]:
        directory = output / f"session-{number:03d}"
        evidence = json.loads((directory / "session.json").read_text())
        assert evidence["timesteps_started"] == start
        assert evidence["timesteps_completed"] == start + 8
        assert evidence["sampling_provenance"]["effective_env_seed"] == seed
        assert evidence["optimizer_step_before"] == optimizer_step
        assert evidence["optimizer_step_after"] == optimizer_step + 1
        assert evidence["learning_rates_before"] == [.00003]
        assert evidence["learning_rates_after"] == [.00003]
        assert evidence["training_sampling_intervals"]
        assert evidence["latest_metrics"]["no_trade"] == (evidence["latest_metrics"]["fill_count"] == 0)
        assert 0 <= evidence["latest_metrics"]["average_cash_ratio"] <= 1
        assert PPO.load(directory / "last.zip", device="cpu").num_timesteps == start + 8
        assert (directory / "policy.zip").is_file()
    assert (output / "result.json").is_file()
    assert json.loads((output / "initial.json").read_text())["matches_saved_validation"] is True
    paired = run_trial(source, dataset, tmp_path / "paired", .0003, sessions=2, timesteps=8, device="cpu")
    for low, high in zip(result["history"], paired["history"]):
        assert low["sampling_provenance"] == high["sampling_provenance"]
        assert low["training_sampling_intervals"] == high["training_sampling_intervals"]
        assert high["learning_rates_after"] == [.0003]


def test_trial_rejects_nonempty_output_and_nonexact_rollouts(source_case, tmp_path):
    from scripts.run_policy_lr_trial import run_trial

    source, dataset, _ = source_case
    with pytest.raises(ValueError, match="empty"):
        run_trial(source, dataset, source, .0003, sessions=1, timesteps=8, device="cpu")
    with pytest.raises(ValueError, match="rollout"):
        run_trial(source, dataset, tmp_path / "invalid", .0003, sessions=1, timesteps=9, device="cpu")


def test_trial_rejects_changed_dataset_and_tampered_initial_metrics(source_case, tmp_path):
    from scripts.run_policy_lr_trial import run_trial
    from quanto.learning.artifacts import write_manifest

    source, dataset, manifest = source_case
    manifest["metrics"]["validation"]["net_log_return"] += 1
    write_manifest(source, {key: value for key, value in manifest.items() if key not in {"id", "files"}})
    with pytest.raises(ValueError, match="initial validation"):
        run_trial(source, dataset, tmp_path / "metrics", .0003, sessions=1, timesteps=8, device="cpu")
    (dataset / "unexpected.txt").write_text("changed dataset")
    with pytest.raises(ValueError, match="dataset content hash"):
        run_trial(source, dataset, tmp_path / "dataset", .0003, sessions=1, timesteps=8, device="cpu")
