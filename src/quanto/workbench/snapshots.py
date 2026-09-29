from __future__ import annotations

import ast
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import tomllib
from typing import Literal
from uuid import uuid4

from quanto.config import BacktestSpec, validate_parameters
from quanto.data.loaders import load_dataset
from quanto.workbench.models import ExperimentDraft, FrozenRun
from quanto.workbench.revisions import digest, json_bytes, publish_files, source_files, verify_files
from quanto.workbench.store import WorkbenchStore


def _git(args: list[str], root: Path) -> str | None:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _environment(project_root: Path) -> dict:
    project = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = project["project"]["dependencies"]
    versions = {}
    for requirement in requirements:
        name = requirement.split("[", 1)[0].split(">", 1)[0].split("<", 1)[0].split("=", 1)[0].strip()
        versions[name] = importlib.metadata.version(name)
    locks = {}
    for name in ("uv.lock", "requirements.lock", "poetry.lock"):
        path = project_root / name
        if path.is_file():
            locks[name] = digest(path.read_bytes())
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "dependencies": versions, "lock_hashes": locks}


class SnapshotBuilder:
    def __init__(self, store: WorkbenchStore):
        self.store = store

    def freeze(self, experiment: ExperimentDraft, source: Literal["web", "agent"], parent_run_id: str | None, db=None) -> FrozenRun:
        payload = experiment.payload
        dataset = self.store.get_dataset(payload["dataset_version_id"], db)
        strategy = self.store.get_strategy(payload["strategy_revision_id"], db)
        dataset_files = source_files(dataset.path)
        strategy_files = source_files(strategy.path)
        if digest(json_bytes({name: digest(data) for name, data in dataset_files.items()})) != dataset.content_hash:
            raise ValueError("registered dataset hash mismatch")
        if digest(json_bytes({name: digest(data) for name, data in strategy_files.items()})) != strategy.content_hash:
            raise ValueError("registered strategy hash mismatch")
        bundle = load_dataset(dataset.path)
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
        package_root = project_root / "src" / "quanto"
        package_files = {path.relative_to(project_root).as_posix(): path.read_bytes() for path in package_root.rglob("*.py")}
        if any(path.is_symlink() for path in package_root.rglob("*")):
            raise ValueError("symbolic links are not snapshot inputs")
        files = {f"dataset/{name}": data for name, data in dataset_files.items()}
        files.update({f"strategy/{name}": data for name, data in strategy_files.items()})
        files.update(package_files)
        for name in ("pyproject.toml", "uv.lock"):
            path = project_root / name
            if path.is_file():
                files[name] = path.read_bytes()
        if {name: path.read_bytes() for name, path in ((key, project_root / key) for key in package_files)} != package_files:
            raise ValueError("engine changed during freeze")
        if source_files(dataset.path) != dataset_files or source_files(strategy.path) != strategy_files:
            raise ValueError("registered input changed during freeze")
        run_id = str(uuid4())
        file_hashes = {name: digest(data) for name, data in files.items()}
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "experiment_id": experiment.id,
            "experiment_revision": experiment.revision,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "parent_run_id": parent_run_id,
            "dataset_version_id": dataset.id,
            "dataset_content_hash": dataset.content_hash,
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
            "environment": _environment(project_root),
            "files": file_hashes,
            "reproducibility": {"mode": "non_exact" if non_exact_reasons else "exact_inputs", "reasons": sorted(set(non_exact_reasons))},
        }
        files["manifest.json"] = json_bytes(manifest)
        target, written_hashes = publish_files(self.store.root / "snapshots", run_id, files)
        verify_files(target, written_hashes)
        return FrozenRun(run_id, target / "manifest.json", digest(files["manifest.json"]))

    def verify(self, frozen: FrozenRun, exact: bool = True) -> dict:
        if digest(frozen.manifest_path.read_bytes()) != frozen.manifest_hash:
            raise ValueError("manifest hash mismatch")
        manifest = json.loads(frozen.manifest_path.read_text(encoding="utf-8"))
        if set(source_files(frozen.manifest_path.parent)) != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("snapshot file set mismatch")
        verify_files(frozen.manifest_path.parent, manifest["files"])
        if exact:
            if manifest["reproducibility"]["mode"] != "exact_inputs":
                raise ValueError("snapshot is not exact-reproducible")
            project_root = Path(__file__).resolve().parents[3]
            if _environment(project_root) != manifest["environment"]:
                raise ValueError("execution environment changed")
        return manifest
