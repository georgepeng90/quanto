"""Resolve immutable framework inputs and report operation capabilities."""
from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from contextlib import nullcontext
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pandas as pd

from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.trading import TradingDefinition
from quanto.config import validate_parameters
from quanto.data.point_in_time import TableSemantics
from quanto.data.providers.akshare import AKShareProvider
from quanto.data.providers.tencent import TencentProvider
from quanto.framework import (CapabilityCheck, CapabilityReport, ModelFrameworkBinding,
                              ResearchInterval, RequirementItem, ResolvedConfiguration,
                              canonical_hash, thaw_json)
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.investing.definitions import LearningDefinition
from quanto.investing.framework import model_request_from_spec, validate_model_operation
from quanto.investing.evaluation import policy_identity
from quanto.learning.artifacts import read_frozen_transform, read_manifest
from quanto.learning.registry import verify_ref
from quanto.objects import digest, json_bytes
from quanto.strategies.base import FixedStrategyDefinition
from quanto.workbench.baskets import BasketStore
from quanto.workbench.bindings import BindingStore
from quanto.workbench.dataset_objects import verify_dataset_version
from quanto.workbench.framework_requests import (FetchRequest, FixedRequest, ModelCreateRequest,
                                                  PolicyBacktestRequest, ResumeRequest)
from quanto.workbench.revisions import source_files


CHECKER_VERSION = "framework-resolver-v1"
_UNSET = object()


class FrameworkBlocked(ValueError):
    def __init__(self, report: CapabilityReport):
        self.report = report
        super().__init__(", ".join(report.error_codes) or "Framework capability blocked")


class FrameworkConflict(ValueError):
    def __init__(self, report: CapabilityReport | None = None):
        self.report = report
        super().__init__("framework conflict: frozen configuration changed before execution")


def recheck_unchanged(frozen: ResolvedConfiguration, check: Callable[[], ResolvedConfiguration]) -> None:
    try:
        current = check()
    except FrameworkBlocked as exc:
        raise FrameworkConflict(exc.report) from exc
    assert_unchanged(frozen, current)


def assert_unchanged(frozen: ResolvedConfiguration, current: ResolvedConfiguration) -> None:
    """Compare the entire resolved decision, excluding its observation time."""
    exclude = {"capability_report": {"generated_at"}}
    if frozen.model_dump(mode="json", exclude=exclude) != current.model_dump(mode="json", exclude=exclude):
        raise FrameworkConflict(current.capability_report)


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


def _db_call(function, *args, db=None, **kwargs):
    return function(*args, **kwargs) if db is None else function(*args, db=db, **kwargs)


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


def _requirement_identity(item: RequirementItem):
    return item.model_dump(mode="json", exclude={"requirement_id", "interval"})


def _model_code(exc: Exception) -> str:
    prefix = str(exc).split(":", 1)[0]
    return prefix if prefix in {"IDENTITY_UNRESOLVED", "BINDING_MISMATCH", "DEFINITION_UNAVAILABLE",
                                "DATA_REQUIREMENT_UNSATISFIED", "TEMPORAL_EVIDENCE_MISSING",
                                "MODEL_IDENTITY_MISMATCH"} else "MODEL_IDENTITY_MISMATCH"


class FrameworkResolver:
    def __init__(self, store, registry=None, *, provider_capabilities=None,
                 supported_table_versions: Collection[tuple[str, str]] = ()):
        from quanto.workbench.definitions import DefinitionRegistry
        self.store = store
        self.registry = registry if registry is not None else DefinitionRegistry()
        self.provider_capabilities = provider_capabilities
        self.supported_table_versions = tuple(supported_table_versions)

    def check(self, request, *, db=None) -> CapabilityReport:
        if isinstance(request, FetchRequest):
            return self._check_fetch(request)
        if isinstance(request, FixedRequest):
            _, report = self._resolve(request, "fixed_backtest", db=db)
            return report
        if isinstance(request, ModelCreateRequest):
            _, report = self._resolve(request, "train", db=db)
            return report
        if isinstance(request, (ResumeRequest, PolicyBacktestRequest)):
            _, report = self._resolve_model_operation(request, db=db)
            return report
        raise TypeError(f"Unsupported framework request: {type(request).__name__}")

    def resolve(self, request: FixedRequest | ModelCreateRequest | ResumeRequest |
                PolicyBacktestRequest, *, db=None) -> ResolvedConfiguration:
        if isinstance(request, FixedRequest):
            resolved, report = self._resolve(request, "fixed_backtest", db=db)
        elif isinstance(request, ModelCreateRequest):
            resolved, report = self._resolve(request, "train", db=db)
        elif isinstance(request, (ResumeRequest, PolicyBacktestRequest)):
            resolved, report = self._resolve_model_operation(request, db=db)
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

    def _resolve(self, request: FixedRequest | ModelCreateRequest, operation: str, *, basket_override=None,
                 dataset_override=None, binding_override=None, strategy_override=None, db=None):
        checks: list[CapabilityCheck] = []
        dataset_id = request.dataset_version_id if operation == "fixed_backtest" else request.dataset_id
        if basket_override is not None:
            if basket_override.ref != request.basket_revision_ref:
                raise ValueError("BINDING_MISMATCH: audited basket revision differs from request")
            basket = basket_override
        else:
            try:
                basket = _db_call(BasketStore(self.store).get, request.basket_revision_ref, db=db)
            except KeyError:
                basket = None
                checks.append(_blocked("basket_reference", "basket_revision_ref", "IDENTITY_UNRESOLVED",
                                       "Basket revision does not exist"))
        try:
            dataset = (dataset_override if dataset_override is not None
                       else _db_call(self.store.get_dataset, dataset_id, db=db))
        except KeyError:
            dataset = None
            checks.append(_blocked("dataset_reference", "dataset_version_id" if operation == "fixed_backtest" else "dataset_id",
                                   "IDENTITY_UNRESOLVED", "Dataset version does not exist"))
        if dataset is not None:
            try:
                binding = (binding_override if binding_override is not None
                           else _db_call(BindingStore(self.store).get_asset_binding, dataset_id, db=db))
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
                strategy = (strategy_override if strategy_override is not None
                            else _db_call(self.store.get_strategy, request.strategy_revision_id, db=db))
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

    def _model_binding(self, model_id: str, *, db=None):
        binding = _db_call(BindingStore(self.store).get_model_binding, model_id, db=db)
        with nullcontext(db) if db is not None else self.store.transaction() as connection:
            row = connection.execute("SELECT dataset_id, spec_json FROM investing_models WHERE id=?", (model_id,)).fetchone()
        if row is None:
            raise ValueError("IDENTITY_UNRESOLVED: model does not exist")
        stored_spec = json.loads(row["spec_json"])
        normalized_spec = PolicyTrainingSpec.model_validate(stored_spec).model_dump(mode="json")
        if (row["dataset_id"] != binding.dataset_version_id or
                canonical_hash(stored_spec) != binding.original_spec_hash or
                normalized_spec != thaw_json(binding.training_configuration.internal_training_spec)):
            raise ValueError("MODEL_IDENTITY_MISMATCH: model row differs from frozen binding")
        original_dataset = _db_call(self.store.get_dataset, binding.dataset_version_id, db=db)
        original_binding = _db_call(BindingStore(self.store).get_asset_binding, binding.dataset_version_id, db=db)
        return self._validate_model_binding(binding, original_dataset, original_binding)

    def _validate_model_binding(self, binding, original_dataset, original_binding):
        if original_dataset.id != binding.dataset_version_id or original_dataset.content_hash != binding.asset_binding.dataset_content_hash:
            raise ValueError("MODEL_IDENTITY_MISMATCH: original dataset differs from frozen binding")
        original_bundle = load_dataset(verify_dataset_version(original_dataset))
        if original_binding != binding.asset_binding:
            raise ValueError("MODEL_IDENTITY_MISMATCH: original dataset binding changed")
        binding.asset_binding.validate_symbols(tuple(original_bundle.instruments.symbol))
        original_prices = next((item for item in binding.asset_binding.table_semantics
                                if item.table == "prices"), None)
        if original_prices is None or original_prices.temporal_rules.get("price_mode") != original_bundle.price_mode:
            raise ValueError("MODEL_IDENTITY_MISMATCH: original dataset price basis differs")
        original = PolicyTrainingSpec.model_validate(thaw_json(binding.original_spec))
        if original.model_dump(mode="json") != thaw_json(binding.training_configuration.internal_training_spec):
            raise ValueError("MODEL_IDENTITY_MISMATCH: frozen original training specification differs")
        if policy_identity(original) != thaw_json(binding.semantic_identity):
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy observation or action identity differs")
        current = {}
        for frozen in binding.training_configuration.definitions:
            try:
                definition = self.registry.get(frozen.ref)
            except KeyError as exc:
                raise ValueError("DEFINITION_UNAVAILABLE: frozen model definition is not registered") from exc
            if self.registry.snapshot(definition, frozen.effective_params) != frozen:
                raise ValueError("MODEL_IDENTITY_MISMATCH: registered definition differs from frozen model")
            current[frozen.ref.kind] = definition
        return binding, original, current

    def _verified_policy_version(self, binding: ModelFrameworkBinding, version_id: str,
                                 original: PolicyTrainingSpec, definitions, *, db=None):
        with nullcontext(db) if db is not None else self.store.transaction() as connection:
            row = connection.execute('''SELECT a.path, a.content_hash, a.files_json, a.manifest_json
                FROM investing_sessions s JOIN jobs j ON j.id=s.job_id
                JOIN learning_artifacts a ON a.id=s.job_id
                WHERE s.model_id=? AND s.job_id=? AND j.state='succeeded' AND a.kind='policy' ''',
                             (binding.model_id, version_id)).fetchone()
        if row is None:
            raise ValueError("MODEL_IDENTITY_MISMATCH: version is not a successful policy of this model")
        ref = {"id": version_id, "path": row["path"], "content_hash": row["content_hash"],
               "files": json.loads(row["files_json"])}
        return self._validate_policy_version_ref(binding, version_id, original, definitions,
                                                 ref, json.loads(row["manifest_json"]))

    def _validate_policy_version_ref(self, binding, version_id, original, definitions, ref, saved_manifest):
        if ref["id"] != version_id:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy version reference differs")
        if digest(json_bytes(ref["files"])) != ref["content_hash"]:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact registry hash differs")
        verify_ref(ref)
        manifest = read_manifest(Path(ref["path"]), "policy")
        if saved_manifest is not None and manifest != saved_manifest:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy artifact differs from saved version")
        if "policy.zip" not in manifest["files"]:
            raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy weights are missing")
        if manifest.get("model_id") != binding.model_id:
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy manifest belongs to another model")
        if (manifest.get("dataset_content_hash") != binding.asset_binding.dataset_content_hash or
                tuple(manifest.get("basket", ())) != tuple(original.backtest.symbols)):
            raise ValueError("MODEL_IDENTITY_MISMATCH: policy training data or basket differs")
        from quanto.investing.rewards import LEGACY_REWARD_VERSION

        actual_semantics = dict(manifest)
        if binding.origin == "legacy_audited":
            actual_semantics = {"reward_version": LEGACY_REWARD_VERSION,
                                "reward_parameters": {}, **actual_semantics}
        for key, value in binding.semantic_identity.items():
            if actual_semantics.get(key) != value:
                raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy semantic version differs")
        previous = PolicyTrainingSpec.model_validate(manifest["config"])
        fixed = original.model_dump(mode="json", exclude={"total_timesteps", "device"})
        if previous.model_dump(mode="json", exclude={"total_timesteps", "device"}) != fixed:
            raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy research configuration differs")
        from quanto.investing.feature_runtime import LearningRuntime

        runtime = LearningRuntime(definitions["learning"], binding.asset_binding, definitions["trading"])
        expected_learning = runtime.identity(previous)
        framework_learning = manifest.get("framework_learning")
        if framework_learning is None:
            artifacts = binding.evidence.get("policy_artifacts", ())
            entries = artifacts.values() if isinstance(artifacts, Mapping) else artifacts
            known = any(isinstance(item, Mapping) and item.get("manifest_id") == manifest["id"]
                        for item in entries)
            if binding.origin != "legacy_audited" or not known:
                raise ValueError("MODEL_IDENTITY_MISMATCH: unbound legacy policy version")
        elif framework_learning != expected_learning:
            raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy framework identity differs")
        descriptor = manifest.get("feature_transform")
        feature = definitions["feature"]
        if feature.fit is None:
            if descriptor is not None:
                raise ValueError("MODEL_IDENTITY_MISMATCH: fixed feature has unexpected transform")
        else:
            if descriptor is None:
                raise ValueError("MODEL_IDENTITY_MISMATCH: fitted feature transform is missing")
            transform = read_frozen_transform(Path(ref["path"]), descriptor)
            runtime.validate_transform(transform, previous)
        return manifest, ref

    def _resolve_model_operation(self, request: ResumeRequest | PolicyBacktestRequest, *, db=None):
        operation = "resume" if isinstance(request, ResumeRequest) else "policy_backtest"
        model_id = request.model_id if operation == "resume" else None
        if operation == "policy_backtest":
            with nullcontext(db) if db is not None else self.store.transaction() as connection:
                row = connection.execute("SELECT model_id FROM investing_sessions WHERE job_id=?",
                                 (request.version_id,)).fetchone()
            if row is None:
                return None, _report(operation, [_blocked("version_reference", "version_id", "IDENTITY_UNRESOLVED",
                                                         "Saved policy version does not exist")])
            model_id = row["model_id"]
            if getattr(request, "model_id", model_id) != model_id:
                return None, _report(operation, [_blocked("version_owner", "model_id", "MODEL_IDENTITY_MISMATCH",
                                                         "Version does not belong to requested model")])
        try:
            binding, original, definitions = _db_call(self._model_binding, model_id, db=db)
            if operation == "resume":
                return _db_call(self._resolve_resume, request, binding, original, definitions, db=db)
            return _db_call(self._resolve_policy_backtest, request, binding, original, definitions, db=db)
        except (KeyError, ValueError, OSError, TypeError) as exc:
            code = "IDENTITY_UNRESOLVED" if isinstance(exc, KeyError) else _model_code(exc)
            path = "model_id" if operation == "resume" else "version_id"
            return None, _report(operation, [_blocked("model_operation", path, code, str(exc))])

    def _resolve_resume(self, request, binding, original, definitions, *, db=None,
                        original_dataset_override=None, parent_id_override=_UNSET, policy_ref_override=None):
        frozen = binding.training_configuration
        create = model_request_from_spec(original, binding.basket, binding.asset_binding,
                                         definitions["learning"].ref, definitions["trading"].ref)
        checked, original_report = self._resolve(
            create, "train", basket_override=binding.basket, db=db,
            dataset_override=original_dataset_override,
            binding_override=binding.asset_binding if original_dataset_override is not None else None)
        if checked is None:
            return None, _report("resume", list(original_report.checks),
                                 request_hash=original_report.request_hash,
                                 dataset_id=original_report.dataset_version_id,
                                 input_hashes=thaw_json(original_report.input_hashes))
        if (checked.request_hash != frozen.request_hash or checked.definitions != frozen.definitions or
                checked.requirements != frozen.requirements or checked.ordered_pairs != frozen.ordered_pairs or
                checked.asset_binding != frozen.asset_binding):
            raise ValueError("MODEL_IDENTITY_MISMATCH: frozen training identity changed")
        if parent_id_override is _UNSET:
            with nullcontext(db) if db is not None else self.store.transaction() as connection:
                row = connection.execute('''SELECT s.job_id FROM investing_sessions s JOIN jobs j ON j.id=s.job_id
                    JOIN learning_artifacts a ON a.id=s.job_id
                    WHERE s.model_id=? AND j.state='succeeded' AND a.kind='policy'
                    ORDER BY j.enqueue_seq DESC LIMIT 1''', (binding.model_id,)).fetchone()
            parent_id = row["job_id"] if row is not None else None
        else:
            parent_id = parent_id_override
        manifest = None
        if parent_id is not None:
            if parent_id_override is _UNSET:
                manifest, _ = _db_call(self._verified_policy_version, binding, parent_id, original, definitions, db=db)
            else:
                if policy_ref_override is None:
                    raise ValueError("MODEL_IDENTITY_MISMATCH: frozen continuation version is missing")
                manifest, _ = self._validate_policy_version_ref(
                    binding, parent_id, original, definitions, policy_ref_override,
                    None)
            if "last.zip" not in manifest["files"]:
                raise ValueError("MODEL_IDENTITY_MISMATCH: saved policy lacks continuation state")
        elif policy_ref_override is not None:
            raise ValueError("MODEL_IDENTITY_MISMATCH: unexpected frozen continuation version")
        values = original.model_dump(mode="json")
        values["total_timesteps"] = request.additional_timesteps or original.total_timesteps
        values["device"] = request.device or original.device
        training = PolicyTrainingSpec.model_validate(values)
        input_hashes = {**thaw_json(original_report.input_hashes), "model_binding": binding.binding_hash,
                        "parent_version": parent_id, "parent_artifact": manifest["id"] if manifest else None}
        request_hash = canonical_hash({"operation": "resume", "request": _request_values(request),
                                       "model_binding": binding.binding_hash, "training": training.model_dump(mode="json"),
                                       "original_request_hash": frozen.request_hash,
                                       "parent_version": parent_id, "parent_artifact": manifest["id"] if manifest else None})
        report = _report("resume", list(original_report.checks), request_hash=request_hash,
                         dataset_id=binding.dataset_version_id, input_hashes=input_hashes,
                         assumptions=original_report.assumptions, limitations=original_report.limitations)
        resolved = ResolvedConfiguration(operation="resume", request=_request_values(request),
                                         basket=binding.basket, asset_binding=binding.asset_binding,
                                         ordered_pairs=binding.ordered_pairs, definitions=frozen.definitions,
                                         requirements=frozen.requirements,
                                         internal_backtest_spec=frozen.internal_backtest_spec,
                                         internal_training_spec=training.model_dump(mode="json"),
                                         request_hash=request_hash, capability_report=report)
        validate_model_operation(binding, request, resolved)
        return resolved, report

    def _resolve_policy_backtest(self, request, binding, original, definitions, *, db=None,
                                 dataset_override=None, binding_override=None, policy_ref_override=None):
        from quanto.config import BacktestSpec
        from quanto.workbench.framework_capabilities import check_requirements

        if policy_ref_override is None:
            manifest, artifact_ref = _db_call(self._verified_policy_version, binding, request.version_id,
                                             original, definitions, db=db)
        else:
            manifest, artifact_ref = self._validate_policy_version_ref(
                binding, request.version_id, original, definitions, policy_ref_override,
                None)
        checks: list[CapabilityCheck] = []
        try:
            dataset = (dataset_override if dataset_override is not None
                       else _db_call(self.store.get_dataset, request.dataset_id, db=db))
            new_binding = (binding_override if binding_override is not None
                           else _db_call(BindingStore(self.store).get_asset_binding, request.dataset_id, db=db))
            bundle = load_dataset(verify_dataset_version(dataset))
            new_binding.validate_symbols(tuple(bundle.instruments.symbol))
        except KeyError as exc:
            raise ValueError("IDENTITY_UNRESOLVED: backtest dataset or binding does not exist") from exc
        if bundle.price_mode != manifest.get("price_mode"):
            checks.append(_blocked("price_mode", "dataset_id", "MODEL_IDENTITY_MISMATCH",
                                   "Saved policy and backtest data use different price modes"))
        try:
            pairs = new_binding.resolve(binding.basket.members)
        except ValueError as exc:
            raise ValueError("MODEL_IDENTITY_MISMATCH: selected canonical assets are absent") from exc
        for old, new in zip(binding.ordered_pairs, pairs):
            if old.instrument_id != new.instrument_id or old.local_symbol != new.local_symbol:
                checks.append(_blocked("selected_assets", "dataset_id", "MODEL_IDENTITY_MISMATCH",
                                       "Selected canonical/local asset order differs from saved policy"))
                break
        if (new_binding.benchmark.instrument_id != binding.asset_binding.benchmark.instrument_id or
                new_binding.benchmark.custom_series_id != binding.asset_binding.benchmark.custom_series_id or
                new_binding.benchmark.return_basis != binding.asset_binding.benchmark.return_basis):
            checks.append(_blocked("benchmark_basis", "dataset_id", "MODEL_IDENTITY_MISMATCH",
                                   "Benchmark identity or return basis differs from training"))
        old_tables = {item.table: item for item in binding.asset_binding.table_semantics}
        new_tables = {item.table: item for item in new_binding.table_semantics}
        required_fields: dict[str, set[str]] = {}
        for requirement in binding.training_configuration.requirements:
            required_fields.setdefault(requirement.table, set()).update(field.name for field in requirement.fields)
        for table, fields in required_fields.items():
            old, new = old_tables.get(table), new_tables.get(table)
            if old is None or new is None:
                checks.append(_blocked("table_semantics", f"asset_binding.table_semantics.{table}",
                                       "MODEL_IDENTITY_MISMATCH", "Required table semantics are absent"))
                continue
            if (old.semantic_id != new.semantic_id or old.version != new.version or
                    thaw_json(old.temporal_rules) != thaw_json(new.temporal_rules) or
                    any(old.schema_definition.get(field) != new.schema_definition.get(field) for field in fields)):
                checks.append(_blocked("table_semantics", f"asset_binding.table_semantics.{table}",
                                       "MODEL_IDENTITY_MISMATCH", "Required field, unit, availability, or price semantics differ"))
        table_readers = []
        for index, declaration in enumerate(new_binding.table_semantics):
            if declaration.table not in required_fields:
                continue
            try:
                table_readers.append(TableSemantics.from_binding(declaration,
                                     supported_versions=self.supported_table_versions))
            except ValueError as exc:
                checks.append(_blocked("table_semantics", f"asset_binding.table_semantics.{index}",
                                       _model_code(exc), str(exc)))
        values = original.backtest.model_dump(mode="json")
        values.update(start=request.spec.start.isoformat(), end=request.spec.end.isoformat())
        for name in definitions["trading"].scalable_costs:
            if name not in values["execution"]:
                checks.append(_blocked("scalable_cost", f"trading.scalable_costs.{name}",
                                       "DEFINITION_UNAVAILABLE", "Declared scalable cost is absent from execution"))
            else:
                values["execution"][name] *= request.spec.cost_multiplier
        backtest = BacktestSpec.model_validate(values)
        calendar_start = bundle.calendar.date.min().date()
        if calendar_start > backtest.start:
            checks.append(_blocked("calendar_coverage", "spec.start", "DATA_REQUIREMENT_UNSATISFIED",
                                   "Backtest dataset calendar starts after the requested interval"))
            calendar_start = backtest.start
        interval = ResearchInterval(start=backtest.start, end=backtest.end, warmup_start=calendar_start)
        frozen = binding.training_configuration
        by_kind = {item.ref.kind: item for item in frozen.definitions}
        requirements = list(definitions["trading"].resolve_requirements(
            by_kind["trading"].effective_params, binding.basket, interval))
        requirements.extend(definitions["learning"].resolve_requirements(
            by_kind["learning"].effective_params, binding.basket, interval))
        requirements.extend(definitions["feature"].resolve_requirements(
            by_kind["feature"].effective_params, binding.basket, interval))
        requirements, conflicts = _deduplicate(requirements)
        checks.extend(conflicts)
        frozen_requirements = {item.requirement_id.split(":", 1)[-1]: _requirement_identity(item)
                               for item in frozen.requirements}
        for item in requirements:
            if frozen_requirements.get(item.requirement_id) != _requirement_identity(item):
                checks.append(_blocked("requirement_identity", f"requirements.{item.requirement_id}",
                                       "MODEL_IDENTITY_MISMATCH", "Required feature or execution contract differs"))
        if request.spec.start <= original.validation_end and request.spec.evaluation_purpose != "development_replay":
            checks.append(_blocked("evaluation_purpose", "spec.evaluation_purpose", "MODEL_IDENTITY_MISMATCH",
                                   "Backtests starting by validation cutoff require development_replay"))
        if not any(item.code == "MODEL_IDENTITY_MISMATCH" for item in checks):
            checks.extend(check_requirements(bundle, new_binding, pairs, requirements,
                                             definitions["trading"], backtest,
                                             table_semantics=tuple(table_readers)))
        hashes = {"basket": binding.basket.content_hash, "dataset": dataset.content_hash,
                  "binding": new_binding.binding_hash, "model_binding": binding.binding_hash,
                  "policy_version": request.version_id, "policy_content_id": manifest["id"],
                  "policy_artifact": artifact_ref["content_hash"]}
        request_hash = canonical_hash({"operation": "policy_backtest", "request": _request_values(request),
                                       "inputs": hashes, "ordered_pairs": [item.model_dump(mode="json") for item in pairs],
                                       "definitions": [item.model_dump(mode="json") for item in frozen.definitions],
                                       "requirements": [item.model_dump(mode="json") for item in requirements],
                                       "backtest": backtest.model_dump(mode="json")})
        report = _report("policy_backtest", checks, request_hash=request_hash, dataset_id=dataset.id,
                         input_hashes=hashes, limitations=bundle.manifest.limitations)
        if report.status == "blocked":
            return None, report
        resolved = ResolvedConfiguration(operation="policy_backtest", request=_request_values(request),
                                         basket=binding.basket, asset_binding=new_binding,
                                         ordered_pairs=pairs, definitions=frozen.definitions,
                                         requirements=requirements,
                                         internal_backtest_spec=backtest.model_dump(mode="json"),
                                         request_hash=request_hash, capability_report=report)
        validate_model_operation(binding, request, resolved)
        return resolved, report
