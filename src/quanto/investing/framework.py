"""Frozen model inputs and operation-specific identity checks."""
from __future__ import annotations

from quanto.framework import (AssetBinding, DefinitionRef, FrozenBasketRevision,
                              ModelFrameworkBinding, ResolvedConfiguration, canonical_hash, thaw_json)
from quanto.investing.contracts import PolicyTrainingSpec
from quanto.workbench.framework_requests import (ModelCreateRequest, PolicyBacktestRequest,
                                                  ResumeRequest)


def model_request_from_spec(spec: PolicyTrainingSpec, basket: FrozenBasketRevision,
                            asset_binding: AssetBinding, learning_ref: DefinitionRef,
                            trading_ref: DefinitionRef) -> ModelCreateRequest:
    """Translate the model's sole original spec into a public canonical request."""
    pairs = asset_binding.resolve(basket.members)
    symbols = tuple(pair.local_symbol for pair in pairs)
    if tuple(spec.backtest.symbols) != symbols:
        raise ValueError("MODEL_IDENTITY_MISMATCH: original model symbols differ from basket binding")
    by_local = {pair.local_symbol: pair.instrument_id.key for pair in pairs}
    values = spec.model_dump(mode="json")
    backtest = values["backtest"]
    del backtest["symbols"]
    for scenario in backtest["risk"]["stress_scenarios"]:
        shocks = scenario["symbol_shocks"]
        if set(shocks) - by_local.keys():
            raise ValueError("MODEL_IDENTITY_MISMATCH: original risk assets differ from basket binding")
        scenario["symbol_shocks"] = {by_local[key]: value for key, value in shocks.items()}
    return ModelCreateRequest(framework_contract_version="multi-asset-v1",
                              basket_revision_ref=basket.ref, dataset_id=asset_binding.dataset_version_id,
                              learning_definition_ref=learning_ref, trading_definition_ref=trading_ref,
                              spec=values)


def _pairs_without_evidence(pairs):
    return tuple((pair.instrument_id, pair.local_symbol) for pair in pairs)


def _require_same(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(f"MODEL_IDENTITY_MISMATCH: {message}")


def validate_model_operation(binding: ModelFrameworkBinding,
                             request: ResumeRequest | PolicyBacktestRequest,
                             resolved: ResolvedConfiguration) -> None:
    """Check a resolved run against the immutable training identity."""
    frozen = binding.training_configuration
    _require_same(canonical_hash(binding.original_spec) == binding.original_spec_hash,
                  "original training spec hash differs")
    _require_same(resolved.basket == binding.basket, "basket revision differs")
    _require_same(_pairs_without_evidence(resolved.ordered_pairs) ==
                  _pairs_without_evidence(binding.ordered_pairs),
                  "ordered canonical/local assets differ")
    _require_same(resolved.definitions == frozen.definitions, "definition or effective parameters differ")
    original = PolicyTrainingSpec.model_validate(thaw_json(binding.original_spec))
    run_training = resolved.internal_training_spec
    if isinstance(request, ResumeRequest):
        _require_same(request.model_id == binding.model_id and resolved.operation == "resume",
                      "resume model or operation differs")
        _require_same(resolved.asset_binding == binding.asset_binding,
                      "resume dataset identity or binding differs")
        _require_same(resolved.requirements == frozen.requirements,
                      "resume data requirements differ")
        _require_same(resolved.internal_backtest_spec == frozen.internal_backtest_spec,
                      "resume backtest conditions differ")
        _require_same(run_training is not None, "resume training configuration missing")
        unchanged = {key: value for key, value in thaw_json(run_training).items()
                     if key not in {"total_timesteps", "device"}}
        expected = original.model_dump(mode="json", exclude={"total_timesteps", "device"})
        _require_same(unchanged == expected, "resume research conditions differ")
    else:
        _require_same(resolved.operation == "policy_backtest", "policy backtest operation differs")
        _require_same(run_training is None, "policy backtest cannot create a training configuration")
        _require_same(tuple(resolved.internal_backtest_spec["symbols"]) ==
                      tuple(original.backtest.symbols), "policy action space differs")
        run = thaw_json(resolved.internal_backtest_spec)
        expected = original.backtest.model_dump(mode="json")
        for field in ("start", "end"):
            expected[field] = run[field]
        trading = next(item for item in frozen.definitions if item.ref.kind == "trading")
        scalable = tuple(trading.declaration["scalable_costs"])
        for name, value in expected["execution"].items():
            if name not in run["execution"]:
                raise ValueError("MODEL_IDENTITY_MISMATCH: execution condition missing")
            required = value * request.spec.cost_multiplier if name in scalable else value
            _require_same(run["execution"][name] == required,
                          f"execution condition {name} differs")
        for name in ("symbols", "initial_cash", "seed", "risk", "metrics"):
            _require_same(run[name] == expected[name], f"backtest condition {name} differs")
