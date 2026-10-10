from __future__ import annotations

from dataclasses import dataclass
from quanto.framework import ResolvedConfiguration, canonical_hash


_SHARED_EXECUTION_PREFIXES = ("src/quanto/backtest/", "src/quanto/risk/", "src/quanto/data/",
                              "src/quanto/reports/")
_SHARED_EXECUTION_FILES = {"src/quanto/config.py", "src/quanto/universe.py"}
_REQUIRED_EXECUTION_FILES = {"src/quanto/backtest/engine.py", "src/quanto/backtest/ledger.py",
                             "src/quanto/backtest/metrics.py", "src/quanto/risk/policy.py",
                             "src/quanto/reports/backtest_report.py", "src/quanto/config.py"}


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _execution_conditions(provenance: dict | None) -> dict | None:
    if not isinstance(provenance, dict):
        return None
    hashes = provenance.get("source_hashes")
    environment = provenance.get("environment_fingerprint")
    if not isinstance(hashes, dict) or not _is_digest(environment):
        return None
    shared = {name: digest for name, digest in hashes.items()
              if isinstance(name, str) and (name.startswith(_SHARED_EXECUTION_PREFIXES)
                                             or name in _SHARED_EXECUTION_FILES)}
    if not _REQUIRED_EXECUTION_FILES <= shared.keys() or not all(_is_digest(digest) for digest in shared.values()):
        return None
    return {"source_hashes": shared, "environment_fingerprint": environment}


@dataclass(frozen=True)
class Page:
    items: list[dict]
    next_cursor: str | None


@dataclass(frozen=True)
class TablePage(Page):
    total: int


@dataclass(frozen=True)
class LogPage(Page):
    state: str
    completed_through: str | None


@dataclass(frozen=True)
class ComparisonResult:
    runs: list[dict]
    differences: dict[str, list]
    directly_comparable: bool


def framework_metadata(value: dict | None, execution_provenance: dict | None = None) -> dict:
    """Project identity and comparison conditions from one frozen resolution."""
    if not isinstance(value, dict):
        return {"identity_status": "unknown", "framework": None, "comparison_key": None,
                "basket_revision_ref": None, "basket_selection_note": None,
                "ordered_pairs": [], "canonical_assets": [], "asset_types": [],
                "benchmark_identity": None, "definitions": {}, "evaluation_purpose": None,
                "sample_purpose": None,
                "request_hash": None, "conditions": None}
    resolved = ResolvedConfiguration.model_validate(value)
    frozen = resolved.model_dump(mode="json")
    binding = resolved.asset_binding
    spec = frozen["internal_backtest_spec"]
    pairs = [{"instrument_id": item.instrument_id.key, "local_symbol": item.local_symbol,
              "evidence_ref": item.evidence_ref} for item in resolved.ordered_pairs]
    benchmark = binding.benchmark
    benchmark_identity = (benchmark.instrument_id.key if benchmark.instrument_id is not None
                          else benchmark.custom_series_id)
    definitions = {item["ref"]["kind"]: {key: item[key] for key in ("ref", "implementation_hash", "effective_params")}
                   for item in frozen["definitions"]}
    request = frozen["request"]
    evaluation_purpose = (request.get("spec") or {}).get("evaluation_purpose")
    table_semantics = [{key: item[key] for key in ("table", "semantic_id", "version", "schema_definition", "temporal_rules")}
                       for item in frozen["asset_binding"]["table_semantics"]]
    execution = _execution_conditions(execution_provenance)
    conditions = {
        "ordered_instruments": [item["instrument_id"] for item in pairs],
        "instruments": sorted(item["instrument_id"] for item in pairs),
        "benchmark": {"identity": benchmark_identity, "return_basis": benchmark.return_basis},
        "dataset_content_hash": binding.dataset_content_hash,
        "table_semantics": sorted(table_semantics, key=lambda item: item["table"]),
        "period": {"start": spec["start"], "end": spec["end"]},
        "evaluation_purpose": evaluation_purpose,
        "sample_purpose": request.get("sample_purpose"),
        "trading_definition": definitions.get("trading"),
        "execution": spec["execution"], "risk": spec["risk"],
        "initial_cash": spec["initial_cash"], "price_mode": next(
            (item["temporal_rules"].get("price_mode") for item in frozen["asset_binding"]["table_semantics"]
             if item["table"] == "prices"), None),
        "metrics": spec["metrics"],
        "shared_execution": execution,
    }
    # Params matter only when they affect the trading definition; method-specific
    # learning, feature and strategy identities remain visible outside this key.
    key = canonical_hash(conditions) if execution is not None else None
    return {"identity_status": "proven", "framework": frozen,
            "basket_revision_ref": resolved.basket.ref.model_dump(mode="json"),
            "basket_selection_note": resolved.basket.selection_note,
            "ordered_pairs": pairs, "canonical_assets": conditions["instruments"],
            "asset_types": sorted({item.instrument_id.instrument_type for item in resolved.ordered_pairs}),
            "benchmark_identity": conditions["benchmark"], "definitions": definitions,
            "evaluation_purpose": evaluation_purpose, "sample_purpose": request.get("sample_purpose"),
            "request_hash": resolved.request_hash,
            "conditions": conditions, "comparison_key": key}


def frozen_result_matches(input_framework: dict, saved_framework: dict,
                          input_provenance: dict | None, saved_provenance: dict | None) -> bool:
    """Check the full frozen identity and execution inputs, including method-specific source files."""
    return (input_framework["framework"] == saved_framework["framework"]
            and isinstance(input_provenance, dict) and isinstance(saved_provenance, dict)
            and all(input_provenance.get(name) == saved_provenance.get(name)
                    for name in ("source_hashes", "environment_fingerprint")))


def compare_runs(run_ids: list[dict]) -> ComparisonResult:
    """Compare already hydrated run summaries; do not recalculate metrics."""
    if not 2 <= len(run_ids) <= 20:
        raise ValueError("comparison requires between 2 and 20 runs")
    fields = ("method", "strategy_revision_id", "strategy_source_hashes", "effective_strategy_params", "dataset_version_id",
              "benchmark", "start", "end", "costs", "price_mode", "metric_definition_version", "metric_definitions",
              "engine_source_hashes", "environment_fingerprint", "seed", "risk", "initial_cash", "symbols", "evaluation", "backtest_spec",
              "execution_definition_version", "risk_definition_version", "identity_status", "ordered_pairs",
              "canonical_assets", "benchmark_identity", "definitions", "basket_revision_ref",
              "basket_selection_note", "evaluation_purpose", "sample_purpose", "request_hash", "comparison_key",
              "execution_provenance", "conditions")
    differences = {}
    for field in fields:
        values = [run.get(field) for run in run_ids]
        if any(value != values[0] for value in values[1:]):
            differences[field] = values
    keys = [run.get("comparison_key") for run in run_ids]
    metric_definitions = [run.get("metric_definitions") for run in run_ids]
    shared_metrics = (set.intersection(*(set(value) for value in metric_definitions))
                      if all(isinstance(value, dict) for value in metric_definitions) else set())
    metric_conflict = any(any(value[name] != metric_definitions[0][name]
                              for value in metric_definitions[1:]) for name in shared_metrics)
    comparable = (all(run.get("state") == "succeeded" and run.get("identity_status") == "proven" for run in run_ids)
                  and keys[0] is not None and all(key == keys[0] for key in keys[1:])
                  and not metric_conflict)
    return ComparisonResult(run_ids, differences, comparable)
