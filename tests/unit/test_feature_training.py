from dataclasses import replace
import shutil

import pandas as pd
import pytest

from quanto.backtest.dataset_identity import standard_table_semantics
from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.trading import ETF_TRADING_DEFINITION
from quanto.data.contracts import InstrumentId
from quanto.framework import AssetBinding, AssetMapping, BenchmarkBinding
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.definitions import etf_learning_definitions
from quanto.investing.evaluation import policy_identity
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from tests.fixtures.investing_training_cases import investing_case


def _case(tmp_path):
    from quanto.investing.feature_runtime import LearningRuntime

    dataset_dir = tmp_path / "dataset"
    spec, dates = investing_case(dataset_dir)
    spec.update(total_timesteps=8, n_steps=4, batch_size=4, eval_every_timesteps=4)
    config = PolicyTrainingSpec.model_validate(spec)
    bundle = load_dataset(dataset_dir)
    dataset_hash = digest(json_bytes(hash_files(dataset_dir)))
    binding = AssetBinding(dataset_version_id="fixture-dataset", dataset_content_hash=dataset_hash,
        assets=(AssetMapping(instrument_id=InstrumentId(exchange="SZSE", instrument_type="etf", code="000001"),
                             local_symbol="000001", evidence_ref="fixture:prices"),),
        benchmark=BenchmarkBinding(custom_series_id="fixture-benchmark", return_basis="price_index",
                                   evidence_ref="fixture:benchmark"),
        table_semantics=standard_table_semantics(bundle, "fixture:daily-data"))
    base = next(definition for definition in etf_learning_definitions()
                if (definition.action_version, definition.reward_version) ==
                   (config.action_version, config.reward_version))
    fit_dates = []

    def fit(frames, params):
        fit_dates.append(tuple(frame.asof for frame in frames))
        return {"scale": max(frame.per_symbol["000001"]["return_5"].value for frame in frames)}

    fitted_feature = replace(base.feature_definition, fit=fit,
                             apply=lambda frame, transform, params: frame,
                             transform_recipe="fixture-train-scale-v1")
    runtime = LearningRuntime(replace(base, feature_definition=fitted_feature), binding,
                              ETF_TRADING_DEFINITION)
    return dataset_dir, spec, config, bundle, runtime, fit_dates


def _policy_context(manifest, config):
    return {"schema_version": 1, "model_id": manifest["model_id"],
            "artifact_model_id": manifest["model_id"],
            "dataset_content_hash": manifest["dataset_content_hash"],
            "basket": manifest["basket"], "price_mode": manifest["price_mode"],
            "semantic_identity": policy_identity(config),
            "framework_learning": manifest["framework_learning"]}


def test_first_fit_reads_only_declared_training_dates(tmp_path):
    dataset_dir, spec, config, bundle, runtime, fit_dates = _case(tmp_path)
    transform = runtime.fit_transform(bundle, config)

    assert len(fit_dates) == 1
    assert min(fit_dates[0]) >= config.train_start
    assert max(fit_dates[0]) <= config.train_end
    assert transform.training_end <= config.train_end
    assert set(transform.training_dataset_hashes) == {runtime.binding.dataset_content_hash}
    assert set(transform.training_binding_hashes) == {runtime.binding.binding_hash}
    prices = bundle.prices.copy()
    prices.loc[prices.date.dt.date >= config.validation_start, ["open", "high", "low", "close"]] *= 100
    changed_future = replace(bundle, prices=prices)
    same_training = runtime.fit_transform(changed_future, config)
    assert same_training.data == transform.data
    assert same_training.training_frame_hashes == transform.training_frame_hashes


def test_training_resume_and_backtest_reuse_one_frozen_transform(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy

    dataset_dir, spec, config, bundle, runtime, fit_dates = _case(tmp_path)
    first = train_policy(spec, dataset_dir, tmp_path / "first", lambda event: None, lambda: False,
                         runtime.binding.dataset_content_hash, model_id="fixture-model", learning_runtime=runtime)
    context = _policy_context(first, config)
    assert first["framework_learning"] == runtime.identity(config)
    assert first["feature_transform"]["transform_hash"]
    assert len(fit_dates) == 1
    resumed = train_policy({**spec, "total_timesteps": 4}, dataset_dir, tmp_path / "resumed",
                           lambda event: None, lambda: False, runtime.binding.dataset_content_hash,
                           resume_dir=tmp_path / "first", resume_context=context,
                           model_id="fixture-model", learning_runtime=runtime)
    assert resumed["feature_transform"] == first["feature_transform"]
    assert (tmp_path / "first" / "transform.json").read_bytes() == (tmp_path / "resumed" / "transform.json").read_bytes()
    assert len(fit_dates) == 1
    backtest_policy(tmp_path / "first", dataset_dir,
                    {"start": str(config.validation_start), "end": str(config.validation_end),
                     "evaluation_purpose": "development_replay"},
                    tmp_path / "report", lambda event: None, lambda: False,
                    learning_runtime=runtime, policy_context=context)
    assert len(fit_dates) == 1


def test_framework_manifest_requires_matching_runtime_and_verified_transform(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy

    dataset_dir, spec, config, bundle, runtime, fit_dates = _case(tmp_path)
    first = train_policy(spec, dataset_dir, tmp_path / "first", lambda event: None, lambda: False,
                         runtime.binding.dataset_content_hash, model_id="fixture-model", learning_runtime=runtime)
    context = _policy_context(first, config)
    request = {"start": str(config.validation_start), "end": str(config.validation_end),
               "evaluation_purpose": "development_replay"}
    with pytest.raises(ValueError, match="runtime|framework"):
        backtest_policy(tmp_path / "first", dataset_dir, request, tmp_path / "no-runtime",
                        lambda event: None, lambda: False)
    different = replace(runtime, definition=replace(runtime.definition, implementation_hash="f" * 64))
    with pytest.raises(ValueError, match="identity|binding"):
        backtest_policy(tmp_path / "first", dataset_dir, request, tmp_path / "wrong-runtime",
                        lambda event: None, lambda: False, learning_runtime=different,
                        policy_context=context)
    (tmp_path / "first" / "transform.json").write_text("{}")
    with pytest.raises(ValueError, match="hash|integrity"):
        backtest_policy(tmp_path / "first", dataset_dir, request, tmp_path / "tampered",
                        lambda event: None, lambda: False, learning_runtime=runtime,
                        policy_context=context)


def test_new_evaluation_dataset_uses_saved_training_transform(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.investing.evaluation import backtest_policy

    dataset_dir, spec, config, bundle, runtime, fit_dates = _case(tmp_path)
    first = train_policy(spec, dataset_dir, tmp_path / "first", lambda event: None, lambda: False,
                         runtime.binding.dataset_content_hash, model_id="fixture-model", learning_runtime=runtime)
    context = _policy_context(first, config)
    changed_dir = tmp_path / "changed-data"
    shutil.copytree(dataset_dir, changed_dir)
    prices = pd.read_csv(changed_dir / "prices.csv", dtype={"symbol": str})
    future = pd.to_datetime(prices.date).dt.date >= config.validation_start
    prices.loc[future, ["open", "high", "low", "close"]] *= 1.2
    prices.to_csv(changed_dir / "prices.csv", index=False)
    values = runtime.binding.model_dump(mode="json")
    values.update(dataset_version_id="changed-evaluation-dataset",
                  dataset_content_hash=digest(json_bytes(hash_files(changed_dir))), binding_hash="")
    changed_runtime = replace(runtime, binding=AssetBinding.model_validate(values))
    request = {"start": str(config.validation_start), "end": str(config.validation_end),
               "evaluation_purpose": "development_replay"}
    report = backtest_policy(tmp_path / "first", changed_dir, request, tmp_path / "report",
                             lambda event: None, lambda: False, learning_runtime=changed_runtime,
                             policy_context=context)
    assert report["policy_content_id"] == first["id"]
    assert len(fit_dates) == 1


def test_cancelled_first_training_does_not_publish_transform(tmp_path):
    from quanto.investing.training import train_policy
    from quanto.learning.contracts import LearningCancelled

    dataset_dir, spec, config, bundle, runtime, fit_dates = _case(tmp_path)
    cancelled = False

    def emit(event):
        nonlocal cancelled
        if event.get("message") == "PPO training started":
            cancelled = True

    with pytest.raises(LearningCancelled):
        train_policy(spec, dataset_dir, tmp_path / "cancelled", emit, lambda: cancelled,
                     runtime.binding.dataset_content_hash, learning_runtime=runtime)
    assert len(fit_dates) == 1
    assert not (tmp_path / "cancelled" / "manifest.json").exists()
    assert not (tmp_path / "cancelled" / "transform.json").exists()
