"""Recent-window reward trial preparation and paired report validation."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quanto.learning.artifacts import write_manifest
from tests.fixtures.investing_training_cases import investing_case
from quanto.investing.rewards import reward_parameters


def comparison_module():
    path = Path(__file__).parents[1] / "scripts/compare_policy_rewards.py"
    spec = importlib.util.spec_from_file_location("compare_policy_rewards", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def long_dataset(root, *, start="2022-01-03", end="2026-09-30", suspended_last=False):
    spec, _ = investing_case(root)
    dates = pd.bdate_range(start, end)
    close = 10 * np.exp(np.arange(len(dates)) * .0001 + np.sin(np.arange(len(dates))) * .003)
    pd.DataFrame(dict(date=dates, symbol="000001", open=close * .999, high=close * 1.01,
                      low=close * .99, close=close, volume=100000, amount=100000000)).to_csv(root / "prices.csv", index=False)
    pd.DataFrame(dict(date=dates, close=4000 + np.arange(len(dates)), turnover=1e9)).to_csv(root / "benchmark.csv", index=False)
    pd.DataFrame(dict(date=dates)).to_csv(root / "calendar.csv", index=False)
    instruments = pd.read_csv(root / "instruments.csv", dtype={"symbol": str})
    instruments.loc[instruments["symbol"] == "000001", "listing_date"] = str(dates[0].date())
    instruments.to_csv(root / "instruments.csv", index=False)
    status = pd.DataFrame(dict(date=dates, symbol="000001", status="trading", can_buy=True, can_sell=True))
    if suspended_last:
        status.loc[status.index[-1], ["status", "can_buy", "can_sell"]] = ["suspended", False, False]
        prices = pd.read_csv(root / "prices.csv", dtype={"symbol": str})
        prices.iloc[:-1].to_csv(root / "prices.csv", index=False)
    status.to_csv(root / "status.csv", index=False)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(calendar_start=str(dates[0].date()), data_start=str(dates[0].date()),
                    calendar_end=str(dates[-1].date()), data_end=str(dates[-1].date()))
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    spec["backtest"].update(start=str(dates[0].date()), end=str(dates[-1].date()))
    spec.update(train_start=str(dates[0].date()), train_end=str((dates[2] - pd.Timedelta(days=1)).date()),
                validation_start=str(dates[4].date()), validation_end=str(dates[6].date()),
                test_start=None, test_end=None, lookback=5, total_timesteps=17, gamma=.99,
                n_steps=8, batch_size=8, eval_every_timesteps=8)
    spec["backtest"]["metrics"]["risk_free_rate"] = .03
    spec_path = root.parent / "source-spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path, spec, dates


def prepared(tmp_path, years=1, *, suspended_last=False):
    module = comparison_module()
    spec_path, source, dates = long_dataset(tmp_path / "data", suspended_last=suspended_last)
    output = tmp_path / "trial"
    trial = module.prepare_trial(spec_path, spec_path.parent / "data", output, years=years)
    return module, trial, source, dates, output


@pytest.mark.parametrize("years", [1, 2])
def test_prepare_freezes_one_recent_window_and_two_paired_specs(tmp_path, years):
    _, trial, source, dates, output = prepared(tmp_path, years)
    assert trial["years"] == years
    assert trial["cutoff"] == dates[-1].date().isoformat()
    assert trial["validation_end"] == trial["cutoff"]
    assert trial["validation_start"] == (pd.Timestamp(trial["cutoff"]) - pd.DateOffset(years=years) + pd.Timedelta(days=1)).date().isoformat()
    assert trial["train_start"] == source["train_start"]
    assert trial["train_end"] == (pd.Timestamp(trial["validation_start"]) - pd.Timedelta(days=1)).date().isoformat()
    control, dsr = trial["specs"]["control"], trial["specs"]["dsr"]
    assert control["reward_version"] == "net-log-return-with-state-v1"
    assert dsr["reward_version"] == "differential-sharpe-v1"
    assert control["gamma"] == dsr["gamma"] == 1
    assert control["backtest"]["metrics"]["risk_free_rate"] == dsr["backtest"]["metrics"]["risk_free_rate"] == 0
    assert control["selection_metric"] == dsr["selection_metric"] == "validation_sharpe"
    assert control["total_timesteps"] == dsr["total_timesteps"] == source["total_timesteps"] == 17
    assert trial["constraint_overrides"] == {"gamma": {"source": .99, "trial": 1.},
        "risk_free_rate": {"source": .03, "trial": 0.}}
    assert control["test_start"] is dsr["test_start"] is None
    for key in source:
        if key not in {"name", "reward_version", "gamma", "selection_metric", "train_end", "validation_start", "validation_end", "test_start", "test_end", "backtest"}:
            assert control[key] == dsr[key] == source[key]
    assert json.loads((output / "trial.json").read_text(encoding="utf-8"))["dataset_content_hash"] == trial["dataset_content_hash"]
    assert json.loads((output / "control-spec.json").read_text(encoding="utf-8")) == control
    assert json.loads((output / "dsr-spec.json").read_text(encoding="utf-8")) == dsr


def test_prepare_uses_legitimate_suspended_session_as_common_cutoff(tmp_path):
    _, trial, _, dates, _ = prepared(tmp_path, suspended_last=True)
    assert trial["cutoff"] == dates[-1].date().isoformat()


def test_prepare_clamps_leap_day_anniversary_to_month_end(tmp_path):
    module = comparison_module()
    spec_path, _, _ = long_dataset(tmp_path / "data", start="2021-01-04", end="2024-02-29")
    trial = module.prepare_trial(spec_path, spec_path.parent / "data", tmp_path / "trial", years=1)
    assert trial["cutoff"] == "2024-02-29"
    assert trial["validation_start"] == "2023-03-01"


def test_prepare_uses_completed_session_when_calendar_extends_beyond_data(tmp_path):
    module = comparison_module()
    spec_path, _, dates = long_dataset(tmp_path / "data", end="2026-10-02")
    manifest_path = tmp_path / "data" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(data_end="2026-10-04", calendar_end="2026-10-05")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    pd.DataFrame({"date": dates.append(pd.DatetimeIndex(["2026-10-05"]))}).to_csv(
        tmp_path / "data" / "calendar.csv", index=False)
    trial = module.prepare_trial(spec_path, tmp_path / "data", tmp_path / "trial")
    assert trial["cutoff"] == "2026-10-02"
    assert trial["validation_start"] == "2024-10-03"


def test_prepare_requires_training_warmup_for_every_basket_symbol(tmp_path):
    module = comparison_module()
    root = tmp_path / "data"
    spec_path, source, dates = long_dataset(root)
    instruments = pd.read_csv(root / "instruments.csv", dtype={"symbol": str})
    late = instruments.copy()
    late["symbol"], late["listing_date"] = "000002", str(dates[10].date())
    pd.concat([instruments, late]).to_csv(root / "instruments.csv", index=False)
    for name in ("prices", "status"):
        frame = pd.read_csv(root / f"{name}.csv", dtype={"symbol": str})
        late = frame.loc[pd.to_datetime(frame["date"]) >= dates[10]].copy()
        late["symbol"] = "000002"
        pd.concat([frame, late]).to_csv(root / f"{name}.csv", index=False)
    source["backtest"]["symbols"].append("000002")
    spec_path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="train interval lacks.*000002"):
        module.prepare_trial(spec_path, root, tmp_path / "trial")
    assert not (tmp_path / "trial").exists()


def test_prepare_rejects_missing_coverage_before_writing(tmp_path):
    module = comparison_module()
    spec_path, _, _ = long_dataset(tmp_path / "data")
    prices = pd.read_csv(tmp_path / "data" / "prices.csv", dtype={"symbol": str})
    gap = pd.Timestamp("2026-01-05")
    prices.loc[pd.to_datetime(prices["date"]) != gap].to_csv(tmp_path / "data" / "prices.csv", index=False)
    output = tmp_path / "trial"
    with pytest.raises(ValueError, match="coverage|validation|price gap"):
        module.prepare_trial(spec_path, spec_path.parent / "data", output, years=2)
    assert not output.exists()


@pytest.mark.parametrize("mutation, message", [
    (lambda value: value["dsr"].update(dataset_content_hash="0" * 64), "dataset"),
    (lambda value: value["dsr"].update(session_timesteps=24), "timesteps"),
    (lambda value: value["dsr"].update(reward_version="differential-sharpe-wrong"), "config|reward"),
    (lambda value: value["control"]["config"].update(validation_start="2025-10-02"), "config"),
])
def test_compare_rejects_mismatched_or_invalid_policy_artifacts(tmp_path, mutation, message):
    module, trial, _, _, _ = prepared(tmp_path)
    control_dir, dsr_dir = tmp_path / "control-model", tmp_path / "dsr-model"
    control_dir.mkdir(); dsr_dir.mkdir()

    def artifact(folder, role, reward, score):
        (folder / "policy.zip").write_bytes(b"placeholder")
        spec = trial["specs"][role]
        manifest = dict(kind="policy", name=spec["name"], config=spec, model_id=role,
                        dataset_content_hash=trial["dataset_content_hash"], reward_version=reward,
                        feature_version="daily-etf-v3-dsr-state-v1", environment_version="daily-session-reward-state-v2",
                        action_version=spec["action_version"], execution_version="next-open-ledger-v1",
                        basket=spec["backtest"]["symbols"], price_mode="raw_cash",
                        reward_parameters=reward_parameters(reward),
                        timesteps_started=0, timesteps_completed=16, session_timesteps=16,
                        history=[{"timesteps": 8}, {"timesteps": 16}], selection_valid=True,
                        selection_metric="validation_sharpe",
                        metrics={"validation": {"sharpe": score, "net_log_return": .1, "max_drawdown": .2}, "test": None})
        return write_manifest(folder, manifest)

    left = artifact(control_dir, "control", "net-log-return-with-state-v1", 1.)
    right = artifact(dsr_dir, "dsr", "differential-sharpe-v1", 1.1)
    mutation({"control": left, "dsr": right})
    for folder, manifest in ((control_dir, left), (dsr_dir, right)):
        # Recompute the immutable manifest after test mutation.
        manifest.pop("id", None)
        write_manifest(folder, manifest)
    with pytest.raises(ValueError, match=message):
        module.compare_trial(tmp_path / "trial" / "trial.json", control_dir, dsr_dir, tmp_path / "comparison")
    assert not (tmp_path / "comparison").exists()


def test_compare_exports_validation_reports_from_real_policy_artifacts(tmp_path):
    module, trial, _, _, _ = prepared(tmp_path)
    from quanto.investing.training import train_policy
    from quanto.learning.registry import hash_files
    from quanto.objects import digest, json_bytes

    dataset_hash = digest(json_bytes(hash_files(Path(trial["dataset_path"]))))
    final_dirs = {}
    for role in ("control", "dsr"):
        initial = tmp_path / f"{role}-initial"
        final = tmp_path / f"{role}-continued"
        first = train_policy(trial["specs"][role], trial["dataset_path"], initial,
                             lambda _: None, lambda: False, dataset_hash, model_id=role, evaluate_test=False)
        continued_spec = {**trial["specs"][role], "total_timesteps": 8}
        second = train_policy(continued_spec, trial["dataset_path"], final, lambda _: None,
                              lambda: False, dataset_hash, model_id=role, resume_dir=initial, evaluate_test=False)
        assert second["timesteps_started"] == first["timesteps_completed"]
        final_dirs[role] = final
    control_dir, dsr_dir = final_dirs["control"], final_dirs["dsr"]
    comparison = module.compare_trial(tmp_path / "trial" / "trial.json", control_dir, dsr_dir, tmp_path / "comparison")
    assert comparison["winner"] is None
    assert comparison["validation_purpose"] == "model_selection"
    assert (tmp_path / "comparison" / "control" / "summary.json").exists()
    assert (tmp_path / "comparison" / "dsr" / "summary.json").exists()
    assert json.loads((tmp_path / "comparison" / "control" / "summary.json").read_text(encoding="utf-8"))["kind"] == "policy_validation"
    assert comparison["training"]["timesteps_completed"] > 17
    assert comparison["results"]["control"]["training_diagnostics"] is not None
    assert "must not be compared" in comparison["reward_diagnostic_note"]
    assert (tmp_path / "comparison" / "comparison.json").exists()


@pytest.mark.parametrize("control_score, control_valid, expected_winner", [
    (None, False, None), (1., True, None), (.5, True, "dsr"), (1.5, True, "control"),
])
def test_comparison_winner_requires_two_valid_strictly_ordered_scores(
        tmp_path, monkeypatch, control_score, control_valid, expected_winner):
    module, trial, _, _, _ = prepared(tmp_path)
    control_dir, dsr_dir = tmp_path / "control-model", tmp_path / "dsr-model"
    control_dir.mkdir(); dsr_dir.mkdir()
    for folder, role, reward, score, valid in (
            (control_dir, "control", "net-log-return-with-state-v1", control_score, control_valid),
            (dsr_dir, "dsr", "differential-sharpe-v1", 1., True)):
        (folder / "policy.zip").write_bytes(b"placeholder")
        config = trial["specs"][role]
        write_manifest(folder, dict(kind="policy", config=config, model_id=role,
            dataset_content_hash=trial["dataset_content_hash"], reward_version=reward,
            feature_version="daily-etf-v3-dsr-state-v1", environment_version="daily-session-reward-state-v2",
            action_version=config["action_version"], execution_version="next-open-ledger-v1",
            basket=config["backtest"]["symbols"], price_mode="raw_cash",
            reward_parameters=reward_parameters(reward),
            timesteps_started=0, timesteps_completed=16, session_timesteps=16,
            history=[{"timesteps": 8}, {"timesteps": 16}], selection_valid=valid,
            selection_metric="validation_sharpe",
            metrics={"validation": {"sharpe": score, "net_log_return": .1, "max_drawdown": .2}, "test": None}))

    def evaluate(policy_dir, dataset_dir, output_dir, emit, cancelled):
        Path(output_dir).mkdir(parents=True)
        summary = {"kind": "policy_validation", "evaluation_purpose": "model_selection",
                   "metrics": {"sharpe": control_score if policy_dir == control_dir else 1., "net_log_return": .1, "max_drawdown": .2}}
        (Path(output_dir) / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return summary

    monkeypatch.setattr(module, "evaluate_validation_policy", evaluate)
    comparison = module.compare_trial(tmp_path / "trial" / "trial.json", control_dir, dsr_dir, tmp_path / "comparison")
    assert comparison["winner"] == expected_winner
    assert comparison["results"]["control"]["selection_valid"] is control_valid
    assert "prior session boundary" in comparison["limitations"][0]
