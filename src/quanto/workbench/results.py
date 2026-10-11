from __future__ import annotations

from dataclasses import dataclass


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


def compare_runs(run_ids: list[dict]) -> ComparisonResult:
    """Compare already hydrated run summaries; do not recalculate metrics."""
    if not 2 <= len(run_ids) <= 20:
        raise ValueError("comparison requires between 2 and 20 runs")
    fields = ("method", "strategy_revision_id", "strategy_source_hashes", "effective_strategy_params", "dataset_version_id",
              "benchmark", "start", "end", "costs", "price_mode", "metric_definition_version", "metric_definitions",
              "engine_source_hashes", "environment_fingerprint", "seed", "risk", "initial_cash", "symbols", "evaluation", "backtest_spec",
              "execution_definition_version", "risk_definition_version", "ordered_pairs",
              "canonical_assets", "benchmark_identity", "definition_evidence", "basket_revision_ref",
              "evaluation_purpose", "sample_purpose", "comparison_key",
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
    comparable = (all(run.get("state") == "succeeded" for run in run_ids)
                  and keys[0] is not None and all(key == keys[0] for key in keys[1:])
                  and not metric_conflict)
    return ComparisonResult(run_ids, differences, comparable)
