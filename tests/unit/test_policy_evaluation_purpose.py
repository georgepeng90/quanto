"""Saved-policy evaluation purpose and declared cost scaling boundaries."""

from types import SimpleNamespace

import pytest
import torch
from pydantic import ValidationError
from stable_baselines3 import PPO

from quanto.investing import evaluation
from quanto.investing.contracts import PolicyBacktestSpec, PolicyTrainingSpec
from quanto.investing.environment import ACTION_VERSION, ENVIRONMENT_VERSION, EXECUTION_VERSION, feature_version
from tests.fixtures.investing_training_cases import investing_case


def _case(tmp_path, monkeypatch):
    values, dates = investing_case(tmp_path / "data")
    config = PolicyTrainingSpec.model_validate(values)
    manifest = dict(id="policy-content", config=config.model_dump(mode="json"),
                    basket=config.backtest.symbols, dataset_content_hash="a" * 64,
                    price_mode="raw_cash", cutoff=config.validation_end.isoformat(),
                    feature_version=feature_version(config.lookback), action_version=ACTION_VERSION,
                    environment_version=ENVIRONMENT_VERSION, execution_version=EXECUTION_VERSION)
    monkeypatch.setattr(evaluation, "read_manifest", lambda *_: manifest)
    request = dict(start=str(dates[60].date()), end=str(dates[89].date()),
                   baseline="cash", evaluation_purpose="post_selection_backtest")
    return manifest, config, request


def test_backtest_spec_requires_explicit_purpose():
    values = dict(start="2024-04-01", end="2024-04-30")
    with pytest.raises(ValidationError, match="evaluation_purpose"):
        PolicyBacktestSpec.model_validate(values)
    assert PolicyBacktestSpec.model_validate(values | {"evaluation_purpose": "model_selection"}).evaluation_purpose == "model_selection"


def test_public_backtest_rejects_model_selection_and_mislabeled_history(tmp_path, monkeypatch):
    _, config, request = _case(tmp_path, monkeypatch)
    for invalid in (request | {"evaluation_purpose": "model_selection"},
                    request | {"start": config.validation_end.isoformat()}):
        with pytest.raises(ValueError, match="evaluation_purpose|development_replay"):
            evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", invalid,
                                       tmp_path / "result", lambda _: None, lambda: False)
    assert not (tmp_path / "result").exists()


def test_public_replay_reports_requested_purpose_and_boundary(tmp_path, monkeypatch):
    _, config, request = _case(tmp_path, monkeypatch)
    request.update(start=config.validation_start.isoformat(), evaluation_purpose="development_replay")
    summary = evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
                                         tmp_path / "result", lambda _: None, lambda: False)
    assert summary["evaluation_purpose"] == "development_replay"
    assert summary["metadata"]["validation_end"] == config.validation_end.isoformat()


def test_validation_remains_model_selection_and_checks_original_data(tmp_path, monkeypatch):
    manifest, _, _ = _case(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="validation dataset content hash"):
        evaluation.evaluate_validation_policy(tmp_path / "policy", tmp_path / "data",
                                              tmp_path / "result", lambda _: None, lambda: False)
    monkeypatch.setattr(evaluation, "hash_files", lambda *_: {"same": "files"})
    manifest["dataset_content_hash"] = evaluation.digest(evaluation.json_bytes({"same": "files"}))
    monkeypatch.setattr(PPO, "load", lambda *_args, **_kwargs: SimpleNamespace(
        device=torch.device("cpu"), predict=lambda observation, deterministic: ([0, 0], None)))
    summary = evaluation.evaluate_validation_policy(tmp_path / "policy", tmp_path / "data",
                                                    tmp_path / "result", lambda _: None, lambda: False)
    assert summary["evaluation_purpose"] == "model_selection"


def test_cost_multiplier_uses_only_trading_declaration(tmp_path, monkeypatch):
    manifest, config, request = _case(tmp_path, monkeypatch)
    manifest["config"]["backtest"]["execution"]["minimum_commission"] = 5.
    config = PolicyTrainingSpec.model_validate(manifest["config"])
    manifest["framework_learning"] = dict.fromkeys((
        "learning_ref", "learning_definition_hash", "learning_params", "feature_ref",
        "feature_definition_hash", "feature_params", "trading_ref",
        "trading_definition_hash", "ordered_assets"), "bound")
    runtime = SimpleNamespace(
        trading=SimpleNamespace(scalable_costs=("minimum_commission",)),
        definition=SimpleNamespace(feature_definition=SimpleNamespace(fit=None)),
        identity=lambda _: manifest["framework_learning"],
        validate_bundle=lambda *_: None,
    )
    request["cost_multiplier"] = 2

    class CapturedBacktest(Exception):
        pass

    def capture(_, __, backtest, *args, **kwargs):
        assert backtest.execution.minimum_commission == config.backtest.execution.minimum_commission * 2
        assert backtest.execution.commission_rate == config.backtest.execution.commission_rate
        assert backtest.execution.slippage_rate == config.backtest.execution.slippage_rate
        raise CapturedBacktest

    monkeypatch.setattr(evaluation, "evaluate_policy", capture)
    with pytest.raises(CapturedBacktest):
        evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
                                   tmp_path / "result", lambda _: None, lambda: False,
                                   learning_runtime=runtime)
