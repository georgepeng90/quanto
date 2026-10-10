"""Audited legacy policy versions may enter the frozen learning runtime."""

from dataclasses import replace
from datetime import datetime, timezone
from copy import deepcopy

import pytest

from quanto.framework import ModelFrameworkBinding, canonical_hash, thaw_json
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.evaluation import backtest_policy, policy_identity
from quanto.investing.training import train_policy
from quanto.learning.artifacts import write_manifest
from tests.integration.test_framework_capabilities import _training_case


def _case(tmp_path):
    from quanto.investing.feature_runtime import learning_runtime_for
    from quanto.workbench.configuration import FrameworkResolver

    store, request, _ = _training_case(tmp_path)
    resolved = FrameworkResolver(store).resolve(request)
    spec = thaw_json(resolved.internal_training_spec)
    config = PolicyTrainingSpec.model_validate(spec)
    dataset_dir = store.get_dataset(resolved.asset_binding.dataset_version_id).path
    parent = train_policy(spec, dataset_dir, tmp_path / "legacy", lambda _: None,
                          lambda: False, resolved.asset_binding.dataset_content_hash,
                          model_id="audited-model", evaluate_test=False)
    binding = ModelFrameworkBinding(
        model_id="audited-model", origin="legacy_audited", original_spec=spec,
        training_configuration=resolved, semantic_identity=policy_identity(config),
        evidence={"policy_artifacts": {"version-1": {"manifest_id": parent["id"]}}},
        created_at=datetime.now(timezone.utc))
    runtime = learning_runtime_for(binding, resolved)
    return resolved, spec, parent, binding, runtime, dataset_dir


def test_audited_legacy_version_resumes_and_evaluates_without_rewriting_parent(tmp_path):
    resolved, spec, parent, binding, runtime, dataset_dir = _case(tmp_path)
    assert runtime.model_binding == binding
    before = (tmp_path / "legacy" / "manifest.json").read_bytes()
    resumed = train_policy({**spec, "total_timesteps": 4}, dataset_dir, tmp_path / "resumed",
                           lambda _: None, lambda: False, resolved.asset_binding.dataset_content_hash,
                           resume_dir=tmp_path / "legacy", model_id=binding.model_id,
                           learning_runtime=runtime, evaluate_test=False)
    assert resumed["framework_learning"] == runtime.identity(PolicyTrainingSpec.model_validate(spec))
    request = dict(start=spec["test_start"], end=spec["test_end"],
                   evaluation_purpose="post_selection_backtest", baseline="cash")
    summary = backtest_policy(tmp_path / "legacy", dataset_dir, request,
                              tmp_path / "report", lambda _: None, lambda: False,
                              learning_runtime=runtime)
    assert summary["evaluation_purpose"] == "post_selection_backtest"
    assert (tmp_path / "legacy" / "manifest.json").read_bytes() == before


def test_legacy_bridge_requires_matching_audit_manifest_model_and_unfitted_feature(tmp_path):
    _, spec, parent, binding, runtime, dataset_dir = _case(tmp_path)
    assert runtime.audited_legacy_identity(parent) == runtime.identity(PolicyTrainingSpec.model_validate(spec))
    changed_spec = deepcopy(parent)
    changed_spec["config"]["seed"] += 1
    for manifest in (parent | {"id": "unlisted"}, parent | {"model_id": "other"},
                     parent | {"dataset_content_hash": "b" * 64},
                     parent | {"feature_transform": {"path": "transform.json"}}, changed_spec):
        with pytest.raises(ValueError, match="MODEL_IDENTITY_MISMATCH"):
            runtime.audited_legacy_identity(manifest)
    with pytest.raises(ValueError, match="MODEL_IDENTITY_MISMATCH"):
        replace(runtime, model_binding=None).audited_legacy_identity(parent)
    with pytest.raises(ValueError, match="MODEL_IDENTITY_MISMATCH"):
        backtest_policy(tmp_path / "legacy", dataset_dir,
                        dict(start=spec["test_start"], end=spec["test_end"],
                             evaluation_purpose="post_selection_backtest", baseline="cash"),
                        tmp_path / "rejected", lambda _: None, lambda: False,
                        learning_runtime=replace(runtime, model_binding=None))
    fitted = replace(runtime.definition.feature_definition, fit=lambda *_: {},
                     apply=lambda frame, transform, params: frame)
    with pytest.raises(ValueError, match="MODEL_IDENTITY_MISMATCH"):
        replace(runtime, definition=replace(runtime.definition, feature_definition=fitted)).audited_legacy_identity(parent)


def test_runtime_factory_rejects_registered_definition_drift(tmp_path):
    from quanto.investing.feature_runtime import learning_runtime_for
    from quanto.workbench.definitions import DefinitionRegistry

    resolved, _, _, binding, _, _ = _case(tmp_path)
    registry = DefinitionRegistry()
    definitions = [registry.get(item.ref) for item in resolved.definitions]
    changed = [replace(item, implementation_hash="f" * 64) if item.ref.kind == "feature" else item
               for item in definitions]
    with pytest.raises(ValueError, match="MODEL_IDENTITY_MISMATCH"):
        learning_runtime_for(binding, resolved, DefinitionRegistry(changed))


def test_audited_raw_spec_may_omit_defaults_while_retaining_raw_hash(tmp_path):
    from quanto.investing.feature_runtime import learning_runtime_for

    resolved, spec, parent, binding, _, dataset_dir = _case(tmp_path)
    raw = deepcopy(spec)
    for field in ("frequency", "reward_version", "selection_metric"):
        raw.pop(field)
    old_manifest = deepcopy(parent)
    for field in ("frequency", "reward_version", "selection_metric"):
        old_manifest["config"].pop(field)
    old_manifest.pop("id")
    old_manifest.pop("files")
    old_manifest = write_manifest(tmp_path / "legacy", old_manifest)
    raw_binding = ModelFrameworkBinding(
        model_id=binding.model_id, origin="legacy_audited", original_spec=raw,
        training_configuration=resolved, semantic_identity=binding.semantic_identity,
        evidence={"policy_artifacts": {"version-1": {"manifest_id": old_manifest["id"]}}},
        created_at=binding.created_at)
    assert raw_binding.original_spec_hash == canonical_hash(raw)
    assert raw_binding.original_spec != resolved.internal_training_spec
    runtime = learning_runtime_for(raw_binding, resolved)
    assert runtime.audited_legacy_identity(old_manifest) == runtime.identity(PolicyTrainingSpec.model_validate(raw))
    resumed = train_policy({**raw, "total_timesteps": 4}, dataset_dir, tmp_path / "resumed",
                           lambda _: None, lambda: False, resolved.asset_binding.dataset_content_hash,
                           resume_dir=tmp_path / "legacy", model_id=raw_binding.model_id,
                           learning_runtime=runtime, evaluate_test=False)
    assert resumed["parent_content_id"] == old_manifest["id"]
    report = backtest_policy(tmp_path / "legacy", dataset_dir,
                             dict(start=raw["test_start"], end=raw["test_end"],
                                  evaluation_purpose="post_selection_backtest", baseline="cash"),
                             tmp_path / "report", lambda _: None, lambda: False,
                             learning_runtime=runtime)
    assert report["policy_content_id"] == old_manifest["id"]
