import json

import pytest

from tests.fixtures.investing_training_cases import investing_case


def test_selection_contract_defaults_to_net_return_and_accepts_sharpe(tmp_path):
    from quanto.investing.contracts import PolicyTrainingSpec

    spec, _ = investing_case(tmp_path / "data")
    assert PolicyTrainingSpec.model_validate(spec).selection_metric == "validation_net_log_return"
    assert PolicyTrainingSpec.model_validate({**spec, "selection_metric": "validation_sharpe"}).selection_metric == "validation_sharpe"
    with pytest.raises(ValueError):
        PolicyTrainingSpec.model_validate({**spec, "selection_metric": "test_sharpe"})


def test_continuation_restores_last_optimizer_and_retains_better_incumbent(tmp_path, monkeypatch):
    import torch
    from stable_baselines3 import PPO
    import quanto.investing.training as training
    from quanto.objects import file_hash

    spec, dates = investing_case(tmp_path / "data")
    original_evaluate = training.evaluate_policy
    scores = iter([4., 3., 2., 1.])
    evaluated_starts = []

    def scored_evaluate(model, bundle, backtest, *args, **kwargs):
        evaluated_starts.append(backtest.start)
        metrics, result, report = original_evaluate(model, bundle, backtest, *args, **kwargs)
        metrics["net_log_return"] = next(scores)
        return metrics, result, report

    monkeypatch.setattr(training, "evaluate_policy", scored_evaluate)
    first = training.train_policy(spec, tmp_path / "data", tmp_path / "first", lambda event: None,
                                  lambda: False, "a" * 64, model_id="model-1", evaluate_test=False)
    last = PPO.load(tmp_path / "first" / "last.zip", device="cpu")
    best = PPO.load(tmp_path / "first" / "policy.zip", device="cpu")
    assert last._n_updates > best._n_updates
    last_parameters = {name: value.clone() for name, value in last.policy.state_dict().items()}
    last_optimizer = last.policy.optimizer.state_dict()
    original_learn = PPO.learn
    resumed = []

    def inspect_learn(model, *args, **kwargs):
        assert kwargs["reset_num_timesteps"] is False
        assert kwargs["total_timesteps"] == 8
        assert model.num_timesteps == first["timesteps_completed"]
        assert model._n_updates == last._n_updates
        assert model._last_obs is None
        assert all(torch.equal(value, model.policy.state_dict()[name]) for name, value in last_parameters.items())
        state = model.policy.optimizer.state_dict()
        assert state["param_groups"] == last_optimizer["param_groups"]
        for key, values in last_optimizer["state"].items():
            assert all(torch.equal(value, state["state"][key][name]) for name, value in values.items())
        resumed.append(True)
        return original_learn(model, *args, **kwargs)

    monkeypatch.setattr(PPO, "learn", inspect_learn)
    parent_hash = file_hash(tmp_path / "first" / "policy.zip")
    events = []
    second = training.train_policy({**spec, "total_timesteps": 8}, tmp_path / "data", tmp_path / "second",
                                   events.append, lambda: False, "a" * 64, resume_dir=tmp_path / "first",
                                   model_id="model-1", evaluate_test=False)
    assert resumed == [True]
    assert second["parent_content_id"] == first["id"]
    assert second["model_id"] == "model-1"
    assert second["timesteps_started"] == first["timesteps_completed"]
    assert second["session_timesteps"] == 8
    assert second["timesteps_completed"] == first["timesteps_completed"] + 8
    assert second["history"][:len(first["history"])] == first["history"]
    assert second["best_timesteps"] == first["best_timesteps"]
    assert second["best_source_content_id"] == first["id"]
    assert second["metrics"]["validation"] == first["metrics"]["validation"]
    assert second["metrics"]["test"] is None
    assert dates[60].date() not in evaluated_starts
    assert file_hash(tmp_path / "second" / "policy.zip") == parent_hash
    assert file_hash(tmp_path / "first" / "policy.zip") == parent_hash
    advanced = PPO.load(tmp_path / "second" / "last.zip", device="cpu")
    assert advanced._n_updates > last._n_updates
    assert any(not torch.equal(value, advanced.policy.state_dict()[name]) for name, value in last_parameters.items())
    assert all(0 <= event["progress"] <= 1 for event in events if "progress" in event)


@pytest.mark.parametrize("scores, expected_timesteps, valid", [([None, None], 8, False), ([None, -2.], 16, True), ([-2., None], 8, True)])
def test_sharpe_selection_handles_undefined_without_fabricating_score(tmp_path, monkeypatch, scores, expected_timesteps, valid):
    import quanto.investing.training as training
    from stable_baselines3 import PPO

    spec, _ = investing_case(tmp_path / "data")
    original_evaluate = training.evaluate_policy
    scores = iter(scores)

    def scored_evaluate(*args, **kwargs):
        metrics, result, report = original_evaluate(*args, **kwargs)
        metrics["sharpe"] = next(scores)
        return metrics, result, report

    monkeypatch.setattr(training, "evaluate_policy", scored_evaluate)
    manifest = training.train_policy({**spec, "selection_metric": "validation_sharpe"}, tmp_path / "data",
                                     tmp_path / "policy", lambda event: None, lambda: False, "a" * 64,
                                     evaluate_test=False)
    assert manifest["selection_valid"] is valid
    assert manifest["best_timesteps"] == expected_timesteps
    assert manifest["metrics"]["validation"]["sharpe"] == (-2. if valid else None)
    assert PPO.load(tmp_path / "policy" / "policy.zip", device="cpu").num_timesteps == expected_timesteps
    assert manifest["history"][0]["validation_sharpe"] == (None if expected_timesteps == 16 or not valid else -2.)
    assert "NaN" not in (tmp_path / "policy" / "manifest.json").read_text()


def test_resume_requires_unchanged_identity_and_verified_last_state(tmp_path):
    import quanto.investing.training as training
    from quanto.learning.artifacts import write_manifest

    spec, _ = investing_case(tmp_path / "data")
    first = training.train_policy(spec, tmp_path / "data", tmp_path / "first", lambda event: None,
                                  lambda: False, "a" * 64, model_id="model-1", evaluate_test=False)
    for index, (changed, content_hash, model_id) in enumerate([
        ({"learning_rate": .001}, "a" * 64, "model-1"),
        ({"selection_metric": "validation_sharpe"}, "a" * 64, "model-1"),
        ({}, "b" * 64, "model-1"),
        ({}, "a" * 64, "model-2"),
    ]):
        with pytest.raises(ValueError, match="resume|identity|configuration"):
            training.train_policy({**spec, **changed}, tmp_path / "data", tmp_path / f"invalid-{index}",
                                  lambda event: None, lambda: False, content_hash, resume_dir=tmp_path / "first",
                                  model_id=model_id, evaluate_test=False)
    (tmp_path / "first" / "last.zip").unlink()
    write_manifest(tmp_path / "first", {key: value for key, value in first.items() if key not in {"id", "files"}})
    with pytest.raises(ValueError, match="last|resume"):
        training.train_policy(spec, tmp_path / "data", tmp_path / "missing-last", lambda event: None,
                              lambda: False, "a" * 64, resume_dir=tmp_path / "first", model_id="model-1",
                              evaluate_test=False)


def test_validation_sharpe_comes_from_daily_report_and_price_mode_is_frozen(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy, evaluate_policy
    from quanto.backtest.dataset_loaders import load_dataset
    from quanto.config import BacktestSpec

    spec, dates = investing_case(tmp_path / "data")
    metrics, _, report = evaluate_policy(None, load_dataset(tmp_path / "data"), BacktestSpec.model_validate(spec["backtest"]),
                                         lookback=5, baseline="buy_hold")
    assert metrics["sharpe"] == report.metrics["sharpe"]
    train_policy(spec, tmp_path / "data", tmp_path / "policy", lambda event: None, lambda: False, "a" * 64)
    data_manifest = json.loads((tmp_path / "data" / "manifest.json").read_text())
    data_manifest["price_mode"] = "adjusted_research"
    data_manifest["limitations"] = ["Synthetic adjusted research prices"]
    (tmp_path / "data" / "manifest.json").write_text(json.dumps(data_manifest))
    with pytest.raises(ValueError, match="price mode|price_mode"):
        backtest_policy(tmp_path / "policy", tmp_path / "data", dict(start=str(dates[60].date()), end=str(dates[89].date())),
                        tmp_path / "backtest", lambda event: None, lambda: False)


@pytest.mark.parametrize("failure", ["cancel", "last_save"])
def test_unpublished_continuation_leaves_parent_version_unchanged(tmp_path, monkeypatch, failure):
    from stable_baselines3 import PPO
    from quanto.investing.training import train_policy
    from quanto.learning.artifacts import read_manifest
    from quanto.learning.contracts import LearningCancelled

    spec, _ = investing_case(tmp_path / "data")
    first = train_policy(spec, tmp_path / "data", tmp_path / "first", lambda event: None,
                         lambda: False, "a" * 64, model_id="model-1", evaluate_test=False)
    cancelled = False
    original_save = PPO.save

    def failing_save(model, path, *args, **kwargs):
        if str(path).endswith("last.zip"):
            raise OSError("synthetic final-state write failure")
        return original_save(model, path, *args, **kwargs)

    def emit(event):
        nonlocal cancelled
        if event.get("validation_net_log_return") is not None:
            cancelled = True

    if failure == "last_save":
        monkeypatch.setattr(PPO, "save", failing_save)
    with pytest.raises(LearningCancelled if failure == "cancel" else OSError):
        train_policy({**spec, "total_timesteps": 8}, tmp_path / "data", tmp_path / "failed",
                     emit if failure == "cancel" else lambda event: None, lambda: cancelled, "a" * 64,
                     resume_dir=tmp_path / "first", model_id="model-1", evaluate_test=False)
    assert not (tmp_path / "failed" / "manifest.json").exists()
    assert read_manifest(tmp_path / "first", "policy") == first
