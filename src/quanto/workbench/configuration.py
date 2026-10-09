"""Resolve immutable framework inputs and report operation capabilities."""
from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import date, datetime, timezone

import pandas as pd

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.trading import TradingDefinition
from quanto.config import validate_parameters
from quanto.data.point_in_time import TableSemantics
from quanto.data.providers.akshare import AKShareProvider
from quanto.data.providers.tencent import TencentProvider
from quanto.framework import (CapabilityCheck, CapabilityReport, ResearchInterval,
                              RequirementItem, ResolvedConfiguration, canonical_hash, thaw_json)
from quanto.investing.definitions import LearningDefinition
from quanto.objects import digest, json_bytes
from quanto.strategies.base import FixedStrategyDefinition
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from quanto.workbench.dataset_objects import verify_dataset_version
from quanto.workbench.framework_requests import (FetchRequest, FixedRequest, ModelCreateRequest,
                                                  PolicyBacktestRequest, ResumeRequest)
from quanto.workbench.revisions import source_files


CHECKER_VERSION = "framework-resolver-v1"


class FrameworkBlocked(ValueError):
    def __init__(self, report: CapabilityReport):
        self.report = report
        super().__init__(", ".join(report.error_codes) or "Framework capability blocked")


def _blocked(name: str, path: str, code: str, message: str) -> CapabilityCheck:
    return CapabilityCheck(name=name, status="blocked", field_path=path, code=code, message=message)


def _report(operation: str, checks: list[CapabilityCheck], *, request_hash=None, dataset_id=None,
            input_hashes=None, assumptions=(), limitations=()) -> CapabilityReport:
    return CapabilityReport(operation=operation, status="blocked" if any(c.status == "blocked" for c in checks) else "ready",
                            request_hash=request_hash, dataset_version_id=dataset_id,
                            input_hashes=input_hashes or {}, checks=tuple(checks),
                            assumptions=tuple(assumptions), limitations=tuple(limitations),
                            checker_version=CHECKER_VERSION, generated_at=datetime.now(timezone.utc))


def _deduplicate(items: Collection[RequirementItem]) -> tuple[tuple[RequirementItem, ...], tuple[CapabilityCheck, ...]]:
    by_id: dict[str, RequirementItem] = {}
    conflicts = []
    for item in items:
        prior = by_id.get(item.requirement_id)
        if prior is None:
            by_id[item.requirement_id] = item
        elif prior != item:
            conflicts.append(_blocked("requirement_conflict", f"requirements.{item.requirement_id}",
                                      "DATA_REQUIREMENT_UNSATISFIED", "One requirement ID has conflicting declarations"))
    return tuple(by_id.values()), tuple(conflicts)


def _source_hashes(path):
    files = source_files(path)
    hashes = {name: digest(content) for name, content in sorted(files.items())}
    return hashes, digest(json_bytes(hashes))


def _request_values(request):
    # The operation is already part of the enclosing canonical input.
    return request.model_dump(mode="json", exclude={"operation"})


def _training_schedule_checks(bundle, learning: LearningDefinition, training) -> tuple[CapabilityCheck, ...]:
    calendar = pd.DatetimeIndex(pd.to_datetime(bundle.calendar.date)).sort_values().unique()
    checks = []
    splits = ("train", "validation") + (("test",) if training.test_start is not None else ())
    for split in splits:
        start, end = getattr(training, f"{split}_start"), getattr(training, f"{split}_end")
        sessions = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
        decisions = pd.DatetimeIndex(pd.to_datetime(learning.schedule(bundle.calendar, start, end,
                                                                        training.frequency)))
        executable = tuple(day for day in decisions if day in sessions[:-1])
        if not executable:
            checks.append(_blocked("learning_schedule", f"spec.{split}", "DATA_REQUIREMENT_UNSATISFIED",
                                   f"{split} partition has no natural decision with a following execution session"))
        if split != "train":
            continue
        if training.episode_sessions > len(sessions):
            checks.append(_blocked("training_episode", "spec.episode_sessions", "DATA_REQUIREMENT_UNSATISFIED",
                                   "Episode length exceeds the registered training sessions"))
        elif executable and not any(any(sessions[index] <= day < sessions[index + training.episode_sessions - 1]
                                        for day in executable)
                                    for index in range(len(sessions) - training.episode_sessions + 1)):
            checks.append(_blocked("training_episode", "spec.episode_sessions", "DATA_REQUIREMENT_UNSATISFIED",
                                   "No episode fragment contains a decision and following execution"))
    return tuple(checks)


class FrameworkResolver:
    def __init__(self, store, registry=None, *, provider_capabilities=None,
                 supported_table_versions: Collection[tuple[str, str]] = ()):
        from quanto.workbench.definitions import DefinitionRegistry
        self.store = store
        self.registry = registry if registry is not None else DefinitionRegistry()
        self.provider_capabilities = provider_capabilities
        self.supported_table_versions = tuple(supported_table_versions)

    def check(self, request) -> CapabilityReport:
        if isinstance(request, FetchRequest):
            return self._check_fetch(request)
        if isinstance(request, FixedRequest):
            _, report = self._resolve(request, "fixed_backtest")
            return report
        if isinstance(request, ModelCreateRequest):
            _, report = self._resolve(request, "train")
            return report
        if isinstance(request, ResumeRequest):
            return _report("resume", [_blocked("model_resolver", "model_id", "DEFINITION_UNAVAILABLE",
                                              "Model identity resolution is provided by T08")])
        if isinstance(request, PolicyBacktestRequest):
            return _report("policy_backtest", [_blocked("model_resolver", "version_id", "DEFINITION_UNAVAILABLE",
                                                      "Model identity resolution is provided by T08")])
        raise TypeError(f"Unsupported framework request: {type(request).__name__}")

    def resolve(self, request: FixedRequest | ModelCreateRequest) -> ResolvedConfiguration:
        if isinstance(request, FixedRequest):
            resolved, report = self._resolve(request, "fixed_backtest")
        elif isinstance(request, ModelCreateRequest):
            resolved, report = self._resolve(request, "train")
        elif isinstance(request, (ResumeRequest, PolicyBacktestRequest)):
            report = self.check(request)
            resolved = None
        else:
            raise TypeError(f"Unsupported framework request: {type(request).__name__}")
        if resolved is None:
            raise FrameworkBlocked(report)
        return resolved

    def _check_fetch(self, request: FetchRequest) -> CapabilityReport:
        from quanto.backtest.market_dataset import build_data_requirement
        values = _request_values(request)
        requirement = build_data_requirement(values)
        request_hash = canonical_hash({"operation": "fetch", "request": values,
                                       "requirement": requirement.model_dump(mode="json")})
        providers = self.provider_capabilities
        if providers is None:
            providers = (AKShareProvider().capabilities(), TencentProvider().capabilities())
        if isinstance(providers, Mapping):
            providers = tuple(providers.values())
        providers = tuple(item.capabilities() if callable(getattr(item, "capabilities", None)) else item for item in providers)
        checks = []
        for index, data_request in enumerate(requirement.requests):
            if not any(provider.supports(data_request) for provider in providers):
                checks.append(_blocked("provider_capability", f"requirements.requests.{index}",
                                       "DATA_REQUIREMENT_UNSATISFIED", f"No declared provider supports {data_request.kind}"))
        return _report("fetch", checks, request_hash=request_hash)

    def _resolve(self, request: FixedRequest | ModelCreateRequest, operation: str):
        checks: list[CapabilityCheck] = []
        dataset_id = request.dataset_version_id if operation == "fixed_backtest" else request.dataset_id
        try:
            basket = BasketStore(self.store).get(request.basket_revision_ref)
        except KeyError:
            basket = None
            checks.append(_blocked("basket_reference", "basket_revision_ref", "IDENTITY_UNRESOLVED",
                                   "Basket revision does not exist"))
        try:
            dataset = self.store.get_dataset(dataset_id)
        except KeyError:
            dataset = None
            checks.append(_blocked("dataset_reference", "dataset_version_id" if operation == "fixed_backtest" else "dataset_id",
                                   "IDENTITY_UNRESOLVED", "Dataset version does not exist"))
        if dataset is not None:
            try:
                binding = BindingStore(self.store).get_asset_binding(dataset_id)
            except KeyError:
                binding = None
                checks.append(_blocked("asset_binding", "asset_binding", "IDENTITY_UNRESOLVED",
                                       "Dataset has no registered asset binding"))
            except ValueError as exc:
                binding = None
                checks.append(_blocked("asset_binding", "asset_binding", "BINDING_MISMATCH", str(exc)))
        else:
            binding = None

        strategy = strategy_definition = strategy_hashes = None
        if operation == "fixed_backtest":
            try:
                strategy = self.store.get_strategy(request.strategy_revision_id)
            except KeyError:
                checks.append(_blocked("strategy_reference", "strategy_revision_id", "IDENTITY_UNRESOLVED",
                                       "Strategy revision does not exist"))
            if strategy is not None:
                try:
                    strategy_hashes, source_hash = _source_hashes(strategy.path)
                    if source_hash != strategy.content_hash:
                        raise ValueError("Registered strategy source hash differs")
                    strategy_definition = FixedStrategyDefinition(strategy.manifest,
                                                                 strategy.manifest["entrypoint"], source_hash)
                except (OSError, ValueError, KeyError) as exc:
                    checks.append(_blocked("strategy_source", "strategy_revision_id", "BINDING_MISMATCH", str(exc)))

        refs = [("trading_definition_ref", request.trading_definition_ref)]
        if operation == "train":
            refs.append(("learning_definition_ref", request.learning_definition_ref))
        elif strategy_definition is not None and strategy_definition.feature_set_ref is not None:
            refs.append(("strategy_revision_id.feature_set_ref", strategy_definition.feature_set_ref))
        definitions = {}
        for path, ref in refs:
            try:
                definitions[ref.kind] = self.registry.get(ref)
            except KeyError:
                checks.append(_blocked("definition_reference", path, "DEFINITION_UNAVAILABLE",
                                       f"Definition {ref.kind}:{ref.id}@{ref.version} is not registered"))
        if operation == "train" and isinstance(definitions.get("learning"), LearningDefinition):
            feature = definitions["learning"].feature_definition
            try:
                registered_feature = self.registry.get(feature.ref)
            except KeyError:
                checks.append(_blocked("definition_reference", "learning_definition_ref.feature_definition", "DEFINITION_UNAVAILABLE",
                                       "Learning feature definition is not registered"))
            else:
                if registered_feature.definition_hash != feature.definition_hash:
                    checks.append(_blocked("definition_reference", "learning_definition_ref.feature_definition",
                                           "BINDING_MISMATCH", "Registered feature definition differs from learning declaration"))
                definitions["feature"] = feature

        unresolved_references = bool(checks)
        if basket is not None and binding is not None:
            try:
                ordered_pairs = binding.resolve(basket.members)
            except ValueError as exc:
                ordered_pairs = None
                checks.append(_blocked("basket_members", "basket_revision_ref.members", "DATA_REQUIREMENT_UNSATISFIED", str(exc)))
        else:
            ordered_pairs = None

        if unresolved_references:
            return None, _report(operation, checks, dataset_id=dataset_id if dataset is not None else None)

        assert basket is not None and binding is not None and dataset is not None
        by_member = {item.instrument_id.key: item for item in binding.assets}
        ordered_identity = [by_member[member.key].model_dump(mode="json") if member.key in by_member else {
            "instrument_id": member.model_dump(mode="json"), "local_symbol": None, "evidence_ref": None,
        } for member in basket.members]
        hashes = {"basket": basket.content_hash, "dataset": dataset.content_hash,
                  "binding": binding.binding_hash,
                  "definitions": {kind: definition.definition_hash for kind, definition in definitions.items()}}
        if strategy is not None:
            hashes["strategy"] = strategy.content_hash
        canonical_base = {
            "operation": operation, "request": _request_values(request),
            "basket": basket.model_dump(mode="json"), "dataset_version_id": dataset.id,
            "dataset_content_hash": dataset.content_hash, "binding": binding.model_dump(mode="json"),
            "ordered_pairs": ordered_identity,
            "definitions": [{"ref": definition.ref.model_dump(mode="json"),
                             "implementation_hash": definition.implementation_hash,
                             "definition_hash": definition.definition_hash}
                            for _, definition in sorted(definitions.items())],
            "strategy": {"revision_id": strategy.id, "content_hash": strategy.content_hash,
                         "source_hashes": strategy_hashes, "definition_hash": strategy_definition.definition_hash}
                        if strategy is not None else None,
        }
        if ordered_pairs is None:
            return None, _report(operation, checks, request_hash=canonical_hash(canonical_base),
                                 dataset_id=dataset.id, input_hashes=hashes)

        trading: TradingDefinition = definitions["trading"]
        member_types = {member.instrument_type for member in basket.members}
        if len(member_types) > 1:
            checks.append(_blocked("basket_types", "basket_revision_ref.members", "MIXED_BASKET_UNSUPPORTED",
                                   "Mixed instrument baskets have no joint strategy and trading definition"))
        else:
            used = [trading, definitions.get("learning") if operation == "train" else strategy_definition,
                    definitions.get("feature")]
            for definition in used:
                if definition is not None and not member_types.issubset(definition.supported_instrument_types):
                    checks.append(_blocked("definition_asset_types", "basket_revision_ref.members", "DEFINITION_UNAVAILABLE",
                                           "Selected definition does not support this instrument type"))
                    break
        if (len(member_types) == 1 and operation == "fixed_backtest"
                and request.trading_definition_ref not in strategy_definition.allowed_trading_refs):
            checks.append(_blocked("trading_definition", "trading_definition_ref", "DEFINITION_UNAVAILABLE",
                                   "Strategy revision does not allow this trading definition"))
        if (len(member_types) == 1 and operation == "train"
                and trading.ref not in definitions["learning"].allowed_trading_refs):
            checks.append(_blocked("trading_definition", "trading_definition_ref", "DEFINITION_UNAVAILABLE",
                                   "Learning definition does not allow this trading definition"))

        internal_backtest = (request.backtest_spec if operation == "fixed_backtest" else request.spec.backtest).to_spec(ordered_pairs)
        training = request.spec.to_spec(ordered_pairs) if operation == "train" else None
        trading_schema = thaw_json(trading.parameter_schema)
        execution_values = internal_backtest.execution.model_dump(mode="json")
        trading_params = validate_parameters(trading_schema, {
            name: execution_values[name] for name in trading_schema.get("properties", {}) if name in execution_values
        })
        resolved_definitions = [self.registry.snapshot(trading, trading_params)]
        if operation == "fixed_backtest":
            strategy_params = validate_parameters(thaw_json(strategy_definition.parameter_schema),
                                                  thaw_json(request.strategy_params))
            if definitions.get("feature") is not None:
                feature = definitions["feature"]
                feature_params = {target: strategy_params[source]
                                  for target, source in strategy_definition.feature_param_map.items()}
                feature_params = validate_parameters(thaw_json(feature.parameter_schema), feature_params)
                resolved_definitions.append(self.registry.snapshot(feature, feature_params))
        else:
            learning = definitions["learning"]
            learning_params = learning.validate_config(training)
            feature = definitions["feature"]
            feature_params = learning.feature_params(training)
            resolved_definitions.extend((self.registry.snapshot(learning, learning_params),
                                         self.registry.snapshot(feature, feature_params)))

        try:
            root = verify_dataset_version(dataset)
            bundle = load_dataset(root)
        except (OSError, ValueError) as exc:
            checks.append(_blocked("dataset_content", "dataset_version_id" if operation == "fixed_backtest" else "dataset_id",
                                   "BINDING_MISMATCH", str(exc)))
            bundle = None
        if bundle is not None:
            try:
                binding.validate_symbols(tuple(bundle.instruments.symbol))
            except ValueError as exc:
                checks.append(_blocked("asset_binding", "asset_binding.assets", "BINDING_MISMATCH", str(exc)))

        table_semantics = []
        for index, declaration in enumerate(binding.table_semantics):
            try:
                table_semantics.append(TableSemantics.from_binding(declaration,
                                      supported_versions=self.supported_table_versions))
            except ValueError as exc:
                code = "DEFINITION_UNAVAILABLE" if str(exc).startswith("DEFINITION_UNAVAILABLE") else "TEMPORAL_EVIDENCE_MISSING"
                checks.append(_blocked("table_semantics", f"asset_binding.table_semantics.{index}", code, str(exc)))

        calendar_start = (bundle.calendar.date.min().date() if bundle is not None else dataset.manifest["calendar_start"])
        if isinstance(calendar_start, str):
            calendar_start = date.fromisoformat(calendar_start)
        if calendar_start > internal_backtest.start:
            checks.append(_blocked("calendar_coverage", "backtest_spec.start", "DATA_REQUIREMENT_UNSATISFIED",
                                   "Registered calendar begins after the requested run"))
            # ResearchInterval requires warmup_start <= start; this request is blocked either way.
            calendar_start = internal_backtest.start
        requirements = []
        if not any(c.code in {"MIXED_BASKET_UNSUPPORTED", "DEFINITION_UNAVAILABLE"} for c in checks):
            if operation == "fixed_backtest":
                intervals = (("backtest", ResearchInterval(start=internal_backtest.start, end=internal_backtest.end,
                                                             warmup_start=calendar_start)),)
            else:
                intervals = [("train", ResearchInterval(start=training.train_start, end=training.train_end,
                                                          warmup_start=calendar_start)),
                             ("validation", ResearchInterval(start=training.validation_start, end=training.validation_end,
                                                               warmup_start=calendar_start))]
                if training.test_start is not None:
                    intervals.append(("test", ResearchInterval(start=training.test_start, end=training.test_end,
                                                                 warmup_start=calendar_start)))
            for partition, interval in intervals:
                declarations = list(trading.resolve_requirements(trading_params, basket, interval))
                if operation == "fixed_backtest":
                    declarations.extend(strategy_definition.resolve_requirements(strategy_params, basket, interval))
                    if definitions.get("feature") is not None:
                        declarations.extend(feature.resolve_requirements(feature_params, basket, interval))
                else:
                    declarations.extend(learning.resolve_requirements(learning_params, basket, interval))
                    declarations.extend(feature.resolve_requirements(feature_params, basket, interval))
                if operation == "train":
                    declarations = [item.model_copy(update={"requirement_id": f"{partition}:{item.requirement_id}"})
                                    for item in declarations]
                requirements.extend(declarations)
        normalized_requirements, conflicts = _deduplicate(requirements)
        checks.extend(conflicts)
        if operation == "fixed_backtest" and definitions.get("feature") is not None and feature.fit is not None:
            checks.append(_blocked("feature_transform", "strategy_revision_id.feature_set_ref", "DEFINITION_UNAVAILABLE",
                                   "Fixed backtest has no registered fitted feature transform reference"))

        canonical_input = {
            **canonical_base,
            "definitions": [item.model_dump(mode="json") for item in resolved_definitions],
            "requirements": [item.model_dump(mode="json") for item in normalized_requirements],
            "backtest": internal_backtest.model_dump(mode="json"),
            "training": training.model_dump(mode="json") if training is not None else None,
            "strategy": {**canonical_base["strategy"], "effective_params": strategy_params}
                        if strategy is not None else None,
        }
        request_hash = canonical_hash(canonical_input)
        if operation == "train" and bundle is not None:
            checks.extend(_training_schedule_checks(bundle, learning, training))
        if bundle is not None and not any(c.code in {"BINDING_MISMATCH", "DEFINITION_UNAVAILABLE",
                                                   "MIXED_BASKET_UNSUPPORTED"} for c in checks):
            from quanto.workbench.framework_capabilities import check_requirements
            checks.extend(check_requirements(bundle, binding, ordered_pairs, normalized_requirements,
                                             trading, internal_backtest, table_semantics=tuple(table_semantics)))
        assumptions = tuple(item.rules.assumption for item in table_semantics if item.rules.assumption)
        limitations = tuple(bundle.manifest.limitations) if bundle is not None else ()
        report = _report(operation, checks, request_hash=request_hash, dataset_id=dataset.id,
                         input_hashes=hashes, assumptions=assumptions, limitations=limitations)
        if report.status == "blocked":
            return None, report
        resolved = ResolvedConfiguration(operation=operation, request=_request_values(request),
                                         basket=basket, asset_binding=binding, ordered_pairs=ordered_pairs,
                                         definitions=tuple(resolved_definitions), requirements=normalized_requirements,
                                         internal_backtest_spec=internal_backtest.model_dump(mode="json"),
                                         internal_training_spec=training.model_dump(mode="json") if training else None,
                                         request_hash=request_hash, capability_report=report)
        return resolved, report
