from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class DatasetVersion:
    id: str
    path: Path
    content_hash: str
    manifest: dict


@dataclass(frozen=True)
class StrategyRevision:
    id: str
    path: Path
    content_hash: str
    manifest: dict


@dataclass(frozen=True)
class ExperimentDraft:
    id: str
    revision: int
    payload: dict


@dataclass(frozen=True)
class FrozenRun:
    run_id: str
    manifest_path: Path
    manifest_hash: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    job_id: str
    manifest_path: Path
    manifest_hash: str
    source: str
    parent_run_id: str | None
    group_id: str | None
    experiment_id: str
    experiment_revision: int
    state: str = "queued"
    completed_through: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class JobRecord:
    id: str
    kind: Literal["backtest", "demo", "fetch", "import", "validate_strategy"]
    state: str
    run_id: str | None
    enqueue_seq: int
    artifact_root: Path
    pid: int | None = None
    process_start: str | None = None
    heartbeat: str | None = None
    completed_through: str | None = None
    error: str | None = None
    input_path: str | None = None


@dataclass(frozen=True)
class BatchItem:
    experiment_id: str
    expected_revision: int
