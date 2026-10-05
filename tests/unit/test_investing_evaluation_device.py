"""Inference device dispatch on synthetic data; no policy training or GPU allocation."""
import numpy as np
import pandas as pd
import pytest
import torch
from stable_baselines3 import PPO

from quanto.investing import evaluation
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.environment import ACTION_VERSION, ENVIRONMENT_VERSION, EXECUTION_VERSION, feature_version
from tests.fixtures.investing_training_cases import investing_case


def case(tmp_path, monkeypatch, device):
    spec, dates = investing_case(tmp_path / "data")
    config = PolicyTrainingSpec.model_validate({**spec, "device": device})
    manifest = dict(id="policy-content", config=config.model_dump(mode="json"),
        basket=config.backtest.symbols, dataset_content_hash="a" * 64, price_mode="raw_cash",
        cutoff=config.validation_end.isoformat(), feature_version=feature_version(config.lookback),
        action_version=ACTION_VERSION, environment_version=ENVIRONMENT_VERSION, execution_version=EXECUTION_VERSION)
    monkeypatch.setattr(evaluation, "read_manifest", lambda *_: manifest)
    request = dict(start=str(dates[60].date()), end=str(dates[89].date()), baseline="policy")
    return request


class HoldModel:
    def __init__(self, device):
        self.device = torch.device(device)

    def predict(self, observation, deterministic):
        return np.zeros(2, dtype=np.int64), None


@pytest.mark.parametrize("device", ["cpu", "cuda"])
def test_policy_inference_follows_manifest_and_reports_actual_device(tmp_path, monkeypatch, device):
    request = case(tmp_path, monkeypatch, device)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    loaded = []

    def load(path, *, device):
        loaded.append(device)
        return HoldModel("cuda:0" if device == "cuda" else device)

    monkeypatch.setattr(PPO, "load", load)
    summary = evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
        tmp_path / "result", lambda _: None, lambda: False)
    assert loaded == [device]
    assert summary["policy_device"] == ("cuda:0" if device == "cuda" else "cpu")


def test_cuda_policy_fails_before_load_when_cuda_is_unavailable(tmp_path, monkeypatch):
    request = case(tmp_path, monkeypatch, "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(PPO, "load", lambda *_args, **_kwargs: pytest.fail("must not load on CPU"))
    with pytest.raises(ValueError, match="CUDA.*unavailable"):
        evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
            tmp_path / "result", lambda _: None, lambda: False)
    assert not (tmp_path / "result").exists()


def test_cuda_policy_rejects_silent_loader_fallback(tmp_path, monkeypatch):
    request = case(tmp_path, monkeypatch, "cuda")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(PPO, "load", lambda *_args, **_kwargs: HoldModel("cpu"))
    with pytest.raises(ValueError, match="device.*cuda"):
        evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
            tmp_path / "result", lambda _: None, lambda: False)
    assert not (tmp_path / "result").exists()


def test_backtest_uses_saved_cash_action_version(tmp_path, monkeypatch):
    request = {**case(tmp_path, monkeypatch, "cpu"), "baseline": "cash"}
    manifest = evaluation.read_manifest(tmp_path / "policy", "policy")
    manifest["config"]["action_version"] = "hold-target-cash-quartiles-v2"
    manifest["action_version"] = "hold-target-cash-quartiles-v2"
    monkeypatch.setattr(evaluation, "read_manifest", lambda *_: manifest)
    summary = evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
        tmp_path / "result", lambda _: None, lambda: False)
    assert summary["metadata"]["action_version"] == "hold-target-cash-quartiles-v2"


def test_saved_cash_action_replays_as_next_open_liquidation(tmp_path, monkeypatch):
    request = case(tmp_path, monkeypatch, "cpu")
    manifest = evaluation.read_manifest(tmp_path / "policy", "policy")
    manifest["config"]["action_version"] = "hold-target-cash-quartiles-v2"
    manifest["action_version"] = "hold-target-cash-quartiles-v2"
    monkeypatch.setattr(evaluation, "read_manifest", lambda *_: manifest)

    class BuyThenCash:
        device = torch.device("cpu")

        def __init__(self):
            self.decisions = 0

        def predict(self, observation, deterministic):
            self.decisions += 1
            return np.array(([1, 4] if self.decisions == 1 else [2, 4] if self.decisions == 2 else [0, 0])), None

    monkeypatch.setattr(PPO, "load", lambda *_args, **_kwargs: BuyThenCash())
    evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
        tmp_path / "result", lambda _: None, lambda: False)
    fills = pd.read_csv(tmp_path / "result" / "fills.csv")
    positions = pd.read_csv(tmp_path / "result" / "positions.csv")
    assert fills.side.tolist() == ["buy", "sell"]
    assert positions.shares.iloc[-1] == 0


@pytest.mark.parametrize("baseline", ["equal_weight", "buy_hold", "cash"])
def test_baseline_from_cuda_manifest_needs_no_policy_device(tmp_path, monkeypatch, baseline):
    request = {**case(tmp_path, monkeypatch, "cuda"), "baseline": baseline}
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(PPO, "load", lambda *_args, **_kwargs: pytest.fail("baseline must not load a policy"))
    summary = evaluation.backtest_policy(tmp_path / "policy", tmp_path / "data", request,
        tmp_path / "result", lambda _: None, lambda: False)
    assert summary["policy_device"] is None
