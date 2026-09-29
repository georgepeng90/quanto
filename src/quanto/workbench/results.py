from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    items: list[dict]
    next_cursor: str | None


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
    fields = ("strategy_revision_id", "strategy_source_hashes", "effective_strategy_params", "dataset_version_id",
              "benchmark", "start", "end", "costs", "price_mode", "metric_definition_version", "metric_definitions",
              "engine_source_hashes", "environment_fingerprint", "seed", "risk", "initial_cash")
    differences = {}
    for field in fields:
        values = [run.get(field) for run in run_ids]
        if any(value != values[0] for value in values[1:]):
            differences[field] = values
    blockers = {"dataset_version_id", "benchmark", "start", "end", "costs", "price_mode", "metric_definition_version",
                "metric_definitions", "engine_source_hashes", "environment_fingerprint"}
    comparable = all(run.get("state") == "succeeded" for run in run_ids) and not blockers.intersection(differences)
    return ComparisonResult(run_ids, differences, comparable)
