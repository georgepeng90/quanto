import pytest
import torch
from stable_baselines3 import PPO

from quanto.backtest.dataset_loaders import load_dataset
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.environment import DailyETFEnv
from quanto.investing.training import split_spec
from tests.fixtures.investing_training_cases import investing_case


def test_trace_replays_sampling_seeds_and_exposes_cash_only_deterministic_policy(tmp_path):
    from scripts.diagnose_policy_stagnation import trace_metrics

    spec, _ = investing_case(tmp_path / "dataset")
    config = PolicyTrainingSpec.model_validate(spec)
    bundle = load_dataset(tmp_path / "dataset")
    env = DailyETFEnv(bundle, split_spec(config, "train"), lookback=config.lookback)
    model = PPO("MlpPolicy", env, n_steps=8, batch_size=8, seed=42, device="cpu")
    with torch.no_grad():
        model.policy.action_net.weight.zero_()
        model.policy.action_net.bias.copy_(torch.tensor([.2, 0., -2., -2., -2., -2., 2.]))
    before = {key: value.clone() for key, value in model.policy.state_dict().items()}
    fixed = trace_metrics(model, bundle, config)
    assert fixed["total_return"] == 0.
    assert fixed["sharpe"] is None
    assert fixed["fill_count"] == 0
    assert fixed["first_entry_date"] is None
    assert fixed["average_cash_ratio"] == 1.
    assert fixed["hold_count"] == fixed["decision_steps"]
    assert fixed["trace"][0]["hold_probability"] == pytest.approx(.549834, abs=1e-6)
    runs = [trace_metrics(model, bundle, config, deterministic=False, seed=seed) for seed in range(10)]
    repeated = trace_metrics(model, bundle, config, deterministic=False, seed=3)
    assert repeated == runs[3]
    assert any(run["fill_count"] > 0 and run["first_entry_date"] is not None for run in runs)
    assert len({tuple(row["action"][0] for row in run["trace"]) for run in runs}) > 1
    assert all(torch.equal(before[key], value) for key, value in model.policy.state_dict().items())
    env.close()


def test_summary_preserves_undefined_sharpe_and_counts_no_trades():
    from scripts.diagnose_policy_stagnation import summarize_runs

    summary = summarize_runs([
        dict(sharpe=None, total_return=0., fill_count=0, average_cash_ratio=1.),
        dict(sharpe=-1., total_return=-.2, fill_count=1, average_cash_ratio=.5),
        dict(sharpe=1., total_return=.4, fill_count=2, average_cash_ratio=.2),
    ])
    assert summary["count"] == 3
    assert summary["no_trade_count"] == 1
    assert summary["metrics"]["sharpe"] == dict(defined_count=2, median=0., p10=-.8, p90=.8)
    assert summary["metrics"]["total_return"]["median"] == 0.
    null = summarize_runs([dict(sharpe=None, total_return=0., fill_count=0, average_cash_ratio=1.)])
    assert null["metrics"]["sharpe"] == dict(defined_count=0, median=None, p10=None, p90=None)


def test_diagnosis_uses_last_checkpoint_and_rejects_wrong_history_or_source_output(tmp_path):
    from scripts.diagnose_policy_stagnation import diagnose
    from quanto.investing.evaluation import evaluate_policy
    from quanto.investing.training import train_policy
    from quanto.learning.artifacts import read_manifest, write_manifest
    from quanto.learning.registry import hash_files
    from quanto.objects import digest, json_bytes

    dataset, source = tmp_path / "data", tmp_path / "source"
    spec, _ = investing_case(dataset)
    config = PolicyTrainingSpec.model_validate(spec)
    bundle = load_dataset(dataset)
    train_policy(spec, dataset, source, lambda _: None, lambda: False,
                 digest(json_bytes(hash_files(dataset))), evaluate_test=False)
    model = PPO.load(source / "last.zip", device="cpu")
    with torch.no_grad():
        model.policy.action_net.weight.zero_()
        model.policy.action_net.bias.copy_(torch.tensor([20., -20., -20., -20., -20., -20., 20.]))
    model.save(source / "policy.zip")
    with torch.no_grad():
        model.policy.action_net.bias[:2].copy_(torch.tensor([-20., 20.]))
    model.save(source / "last.zip")
    import json
    manifest = json.loads((source / "manifest.json").read_bytes())
    metrics, _, _ = evaluate_policy(model, bundle, split_spec(config, "validation"), lookback=config.lookback)
    manifest["history"][-1].update({f"validation_{key}": metrics[key] for key in ("total_return", "max_drawdown", "sharpe")})
    write_manifest(source, {key: value for key, value in manifest.items() if key not in {"id", "files"}})
    original = hash_files(source)
    summary = diagnose(source, dataset, tmp_path / "result", label="test", repeats=1)
    assert summary["deterministic"]["fill_count"] > 0
    assert summary["deterministic"]["total_return"] > 0.
    assert hash_files(source) == original
    with pytest.raises(ValueError, match="separate"):
        diagnose(source, dataset, source / "new", label="bad")
    manifest = read_manifest(source, "policy")
    manifest["history"][-1]["validation_total_return"] = 99.
    write_manifest(source, {key: value for key, value in manifest.items() if key not in {"id", "files"}})
    with pytest.raises(ValueError, match="disagrees"):
        diagnose(source, dataset, tmp_path / "mismatch", label="bad", repeats=1)
