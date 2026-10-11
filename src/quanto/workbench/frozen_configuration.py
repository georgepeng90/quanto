"""Re-resolve a framework decision using only verified immutable artifacts."""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from quanto.backtest.dataset_loaders import load_dataset
from quanto.framework import ModelFrameworkBinding, ResolvedConfiguration
from quanto.learning.registry import hash_files
from quanto.objects import digest, json_bytes
from quanto.workbench.configuration import FrameworkBlocked, FrameworkResolver, assert_unchanged
from quanto.workbench.framework_requests import (FixedRequest, ModelCreateRequest,
                                                  PolicyBacktestRequest, ResumeRequest)
from quanto.workbench.models import DatasetVersion, StrategyRevision


def _dataset(ref: Mapping, identity: str) -> DatasetVersion:
    if ref["id"] != identity:
        raise ValueError("frozen dataset identity differs from request")
    root = Path(ref["path"])
    files = hash_files(root)
    if files != ref["files"] or digest(json_bytes(files)) != ref["content_hash"]:
        raise ValueError("frozen dataset file or content hash mismatch")
    manifest = load_dataset(root).manifest.model_dump(mode="json")
    return DatasetVersion(identity, root, ref["content_hash"], manifest)


def _strategy(ref: Mapping, identity: str) -> StrategyRevision:
    if ref["id"] != identity:
        raise ValueError("frozen strategy identity differs from request")
    root = Path(ref["path"])
    files = hash_files(root)
    if files != ref["files"] or digest(json_bytes(files)) != ref["content_hash"]:
        raise ValueError("frozen strategy file or content hash mismatch")
    if json.loads((root / "strategy.json").read_bytes()) != ref["manifest"]:
        raise ValueError("frozen strategy declaration differs from its source")
    return StrategyRevision(identity, root, ref["content_hash"], dict(ref["manifest"]))


def verify_frozen_configuration(
    resolved: ResolvedConfiguration, *, dataset_ref: Mapping,
    strategy_ref: Mapping | None = None, model_binding: ModelFrameworkBinding | None = None,
    policy_ref: Mapping | None = None, policy_context: Mapping | None = None,
    training_dataset_ref: Mapping | None = None,
) -> ResolvedConfiguration:
    """Recompute capability with frozen code and data, without a workspace database."""
    resolver = FrameworkResolver(None)
    if resolved.operation == "fixed_backtest":
        request = FixedRequest.model_validate(resolved.request)
        if strategy_ref is None:
            raise ValueError("frozen strategy reference is required")
        current, report = resolver._resolve(
            request, "fixed_backtest", basket_override=resolved.basket,
            dataset_override=_dataset(dataset_ref, request.dataset_version_id),
            binding_override=resolved.asset_binding,
            strategy_override=_strategy(strategy_ref, request.strategy_revision_id),
        )
    elif resolved.operation == "train":
        request = ModelCreateRequest.model_validate(resolved.request)
        current, report = resolver._resolve(
            request, "train", basket_override=resolved.basket,
            dataset_override=_dataset(dataset_ref, request.dataset_id),
            binding_override=resolved.asset_binding,
        )
    elif resolved.operation in {"resume", "policy_backtest"}:
        if model_binding is None:
            raise ValueError("frozen model operation requires a model binding")
        original_ref = training_dataset_ref or dataset_ref
        original_dataset = _dataset(original_ref, model_binding.dataset_version_id)
        binding, original, definitions = resolver._validate_model_binding(
            model_binding, original_dataset, model_binding.asset_binding)
        if resolved.operation == "resume":
            request = ResumeRequest.model_validate(resolved.request)
            if request.model_id != binding.model_id:
                raise ValueError("frozen model identity differs from request")
            if dataset_ref["id"] != binding.dataset_version_id:
                raise ValueError("frozen resume dataset differs from training")
            current, report = resolver._resolve_resume(
                request, binding, original, definitions,
                original_dataset_override=original_dataset,
                parent_id_override=resolved.capability_report.input_hashes.get("parent_version"),
                policy_ref_override=policy_ref, policy_context_override=policy_context)
        else:
            request = PolicyBacktestRequest.model_validate(resolved.request)
            if policy_ref is None:
                raise ValueError("frozen policy version is required")
            current, report = resolver._resolve_policy_backtest(
                request, binding, original, definitions,
                dataset_override=_dataset(dataset_ref, request.dataset_id),
                binding_override=resolved.asset_binding,
                policy_ref_override=policy_ref, policy_context_override=policy_context)
    else:
        raise ValueError("unsupported frozen framework operation")
    if current is None:
        raise FrameworkBlocked(report)
    assert_unchanged(resolved, current)
    return current
