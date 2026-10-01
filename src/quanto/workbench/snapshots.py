from __future__ import annotations

import ast
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tomllib
from typing import Literal
from uuid import uuid4

from quanto.config import BacktestSpec, validate_parameters
from quanto.backtest.dataset_loaders import load_dataset
from quanto.workbench.models import ExperimentDraft, FrozenRun
from quanto.workbench.dataset_objects import ensure_dataset_object, verify_dataset_object
from quanto.workbench.revisions import digest, json_bytes, publish_files, source_files, verify_files
from quanto.workbench.store import WorkbenchStore


def _git(args: list[str], root: Path) -> str | None:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


_METADATA_NAMES = ("pyproject.toml", "uv.lock", "requirements.lock", "poetry.lock")


def _metadata_files(project_root: Path) -> dict[str, bytes]:
    result = {}
    for name in _METADATA_NAMES:
        path = project_root / name
        if path.is_symlink():
            raise ValueError("symbolic links are not snapshot inputs")
        if path.is_file():
            result[name] = path.read_bytes()
    if "pyproject.toml" not in result:
        raise FileNotFoundError(project_root / "pyproject.toml")
    return result


def _execution_files(project_root: Path) -> dict[str, bytes]:
    package_root = project_root / "src" / "quanto"
    if any(path.is_symlink() for path in package_root.rglob("*")):
        raise ValueError("symbolic links are not snapshot inputs")
    files = {path.relative_to(project_root).as_posix(): path.read_bytes() for path in package_root.rglob("*.py")}
    files.update(_metadata_files(project_root))
    return files


def _environment(project_root: Path, metadata_files: dict[str, bytes]) -> dict:
    project = tomllib.loads(metadata_files["pyproject.toml"].decode("utf-8"))
    installed = [list(item) for item in sorted({
        (re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower(), dist.version)
        for dist in importlib.metadata.distributions()
    })]
    locks = {name: digest(content) for name, content in metadata_files.items() if name in _METADATA_NAMES and name != "pyproject.toml"}
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "declared_requirements": project["project"].get("dependencies", []),
        "declared_optional_requirements": project["project"].get("optional-dependencies", {}),
        "installed_distributions": installed,
        "lock_hashes": locks,
    }


class SnapshotBuilder:
    def __init__(self, store: WorkbenchStore):
        self.store = store
        self._batch_data = ContextVar("snapshot_batch_data", default=None)

    @contextmanager
    def batch(self):
        token = self._batch_data.set({})
        try:
            yield
        finally:
            self._batch_data.reset(token)

    def freeze(self, experiment: ExperimentDraft, source: Literal["web", "agent"], parent_run_id: str | None, db=None) -> FrozenRun:
        payload = experiment.payload
        dataset = self.store.get_dataset(payload["dataset_version_id"], db)
        strategy = self.store.get_strategy(payload["strategy_revision_id"], db)
        prepared = self._batch_data.get()
        if prepared is not None and dataset.id in prepared:
            dataset_hashes, bundle = prepared[dataset.id]
        else:
            object_path, dataset_hashes = ensure_dataset_object(self.store.root, dataset)
            bundle = load_dataset(object_path)
            if prepared is not None:
                prepared[dataset.id] = (dataset_hashes, bundle)
        strategy_files = source_files(strategy.path)
        if digest(json_bytes({name: digest(data) for name, data in strategy_files.items()})) != strategy.content_hash:
            raise ValueError("registered strategy hash mismatch")
        spec = BacktestSpec.model_validate(payload["backtest_spec"])
        spec.validate_for_dataset(bundle)
        original_params = payload.get("strategy_params", {})
        effective_params = validate_parameters(strategy.manifest["parameter_schema"], original_params)
        for name in strategy.manifest.get("research_data", []):
            if name not in bundle.manifest.auxiliary_tables:
                raise ValueError(f"unregistered research data: {name}")
        non_exact_reasons = []
        if strategy.manifest.get("external_stateful"):
            non_exact_reasons.append("declared external state")
        for name, content in strategy_files.items():
            if not name.endswith(".py"):
                continue
            for node in ast.walk(ast.parse(content, filename=name)):
                if not isinstance(node, ast.Call):
                    continue
                call_name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                if call_name in {"__import__", "import_module", "open", "read_text", "read_bytes"}:
                    non_exact_reasons.append(f"dynamic import or external read in {name}")
        project_root = Path(__file__).resolve().parents[3]
        execution_files = _execution_files(project_root)
        files = {f"strategy/{name}": data for name, data in strategy_files.items()}
        files.update(execution_files)
        environment = _environment(project_root, execution_files)
        if _execution_files(project_root) != execution_files:
            raise ValueError("engine or environment files changed during freeze")
        if source_files(strategy.path) != strategy_files:
            raise ValueError("registered input changed during freeze")
        run_id = str(uuid4())
        file_hashes = {name: digest(data) for name, data in files.items()}
        if any(file_hashes[name] != expected for name, expected in environment["lock_hashes"].items()):
            raise ValueError("lock hash differs from frozen input")
        manifest = {
            "schema_version": 2,
            "run_id": run_id,
            "experiment_id": experiment.id,
            "experiment_revision": experiment.revision,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "parent_run_id": parent_run_id,
            "dataset_version_id": dataset.id,
            "dataset_content_hash": dataset.content_hash,
            "dataset_ref": {"version_id": dataset.id, "content_hash": dataset.content_hash, "files": dataset_hashes},
            "dataset_manifest": bundle.manifest.model_dump(mode="json"),
            "strategy_revision_id": strategy.id,
            "strategy_content_hash": strategy.content_hash,
            "strategy_manifest": strategy.manifest,
            "original_strategy_params": original_params,
            "effective_strategy_params": effective_params,
            "backtest_spec": spec.model_dump(mode="json"),
            "sample_purpose": payload["sample_purpose"],
            "warmup_data_start": bundle.manifest.calendar_start.isoformat(),
            "performance_start": spec.start.isoformat(),
            "performance_end": spec.end.isoformat(),
            "seed": spec.seed,
            "metric_definition_version": spec.metrics.definition_version,
            "execution_definition_version": 1,
            "risk_definition_version": 1,
            "git_commit": _git(["rev-parse", "HEAD"], project_root),
            "git_status": _git(["status", "--porcelain=v1", "--untracked-files=no"], project_root),
            "environment": environment,
            "files": file_hashes,
            "reproducibility": {"mode": "non_exact" if non_exact_reasons else "exact_inputs", "reasons": sorted(set(non_exact_reasons))},
        }
        market_source = self.store.dataset_market_source(dataset.id, db)
        if market_source is not None:
            manifest["market_snapshot_ref"] = market_source
        files["manifest.json"] = json_bytes(manifest)
        target, written_hashes = publish_files(self.store.root / "snapshots", run_id, files)
        verify_files(target, written_hashes)
        if _execution_files(project_root) != execution_files:
            raise ValueError("engine or environment files changed during freeze")
        if source_files(strategy.path) != strategy_files:
            raise ValueError("registered input changed during freeze")
        return FrozenRun(run_id, target / "manifest.json", digest(files["manifest.json"]))

    def verify(self, frozen: FrozenRun, exact: bool = True) -> dict:
        if digest(frozen.manifest_path.read_bytes()) != frozen.manifest_hash:
            raise ValueError("manifest hash mismatch")
        manifest = json.loads(frozen.manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") not in (1, 2):
            raise ValueError("unsupported snapshot schema version")
        if set(source_files(frozen.manifest_path.parent)) != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("snapshot file set mismatch")
        verify_files(frozen.manifest_path.parent, manifest["files"])
        if manifest["schema_version"] == 2:
            ref = manifest["dataset_ref"]
            if ref["version_id"] != manifest["dataset_version_id"] or ref["content_hash"] != manifest["dataset_content_hash"]:
                raise ValueError("dataset reference mismatch")
            verify_dataset_object(self.store.root, ref)
        if any(manifest["files"].get(name) != expected for name, expected in manifest["environment"]["lock_hashes"].items()):
            raise ValueError("lock hash differs from frozen input")
        if exact:
            if manifest["reproducibility"]["mode"] != "exact_inputs":
                raise ValueError("snapshot is not exact-reproducible")
            project_root = Path(__file__).resolve().parents[3]
            if _environment(project_root, _metadata_files(project_root)) != manifest["environment"]:
                raise ValueError("execution environment changed")
        return manifest
