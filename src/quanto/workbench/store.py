from __future__ import annotations

import ast
from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from quanto.config import validate_parameters
from quanto.data.settings import get_market_root
from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.dataset_storage import write_parquet_dataset
from quanto.workbench.models import DatasetVersion, ExperimentDraft, JobRecord, RunRecord, StrategyRevision
from quanto.workbench.revisions import digest, json_bytes, publish_files, safe_name, source_files


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _module_name(filename: str) -> str:
    parts = list(Path(filename).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _local_imports(tree: ast.AST, filename: str) -> set[str]:
    module = _module_name(filename)
    package = module if filename.endswith("/__init__.py") else module.rpartition(".")[0]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                if node.level > len(parts):
                    raise ValueError("relative import escapes strategy package")
                prefix = parts[:len(parts) - node.level + 1]
                base = ".".join([*prefix, *node.module.split(".")] if node.module else prefix)
            else:
                base = node.module or ""
            if base:
                found.add(base)
            found.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names if alias.name != "*")
    return found


class WorkbenchStore:
    def __init__(self, root: Path, artifact_root: Path | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        config_path = self.root / "workspace.json"
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        if artifact_root is None and config_path.exists():
            artifact_root = Path(config["artifact_root"])
            if not artifact_root.is_absolute():
                artifact_root = self.root / artifact_root
        if artifact_root is None:
            artifact_root = self.root.parent.parent / "outputs" / "workbench" if self.root.parts[-2:] == ("data", "workbench") else self.root / "artifacts"
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        if not config_path.exists():
            config_path.write_text(json.dumps({"artifact_root": str(self.artifact_root)}, ensure_ascii=False), encoding="utf-8")
        self.db_path = self.root / "workbench.sqlite3"
        from quanto.workbench.migrations import migrate_schema
        with self.transaction() as db:
            migrate_schema(db)

    @property
    def market_root(self) -> Path:
        return get_market_root()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise RuntimeError("SQLite foreign keys could not be enabled")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register_dataset(self, bundle_dir: Path) -> DatasetVersion:
        source = Path(bundle_dir)
        if source.is_symlink():
            raise ValueError("symbolic links are not snapshot inputs")
        manifest_path = source / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("symbolic links are not snapshot inputs")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        names = {"manifest.json"}
        for table in ("prices", "benchmark", "calendar", "instruments", "status", "events"):
            name = next((f"{table}.{suffix}" for suffix in ("parquet", "csv") if (source / f"{table}.{suffix}").exists()), None)
            if name is None:
                raise FileNotFoundError(source / f"{table}.csv")
            names.add(name)
        names.update(manifest.get("auxiliary_tables", {}).values())
        for name in names:
            safe_name(name)
            path = source / name
            if path.is_symlink() or not path.is_file() or any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents):
                raise ValueError("symbolic links are not snapshot inputs")
        before = {name: (source / name).read_bytes() for name in sorted(names)}
        bundle = load_dataset(source)
        identity = str(uuid4())
        with tempfile.TemporaryDirectory(prefix="quanto-parquet-") as directory:
            normalized = Path(directory)
            write_parquet_dataset(bundle, normalized)
            target, hashes = publish_files(self.root / "datasets", identity, source_files(normalized))
        if {name: (source / name).read_bytes() for name in sorted(names)} != before:
            raise ValueError("dataset changed during registration")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        content_hash = digest(json_bytes(hashes))
        with self.transaction() as db:
            db.execute("INSERT INTO datasets VALUES (?, ?, ?, ?)", (identity, str(target), content_hash, json_bytes(manifest).decode()))
        return DatasetVersion(identity, target, content_hash, manifest)

    def register_strategy(self, files: dict[str, bytes], manifest: dict) -> StrategyRevision:
        if "strategy.json" not in files or json.loads(files["strategy.json"]) != manifest:
            raise ValueError("strategy.json must match the declaration")
        entrypoint = manifest.get("entrypoint", "")
        module_path = entrypoint.partition(":")[0]
        safe_name(module_path)
        if not entrypoint.endswith(":create_strategy") or not module_path.endswith(".py"):
            raise ValueError("invalid strategy entrypoint")
        auxiliary = manifest.get("auxiliary_sources", [])
        if not isinstance(auxiliary, list) or not isinstance(manifest.get("research_data", []), list):
            raise ValueError("invalid strategy declaration")
        declared = {"strategy.json", module_path}
        for name in auxiliary:
            safe_name(name)
            declared.add(name)
        if not declared.issubset(files):
            raise ValueError("missing declared strategy file")
        for name in files:
            safe_name(name)
        for name in declared:
            if name.endswith(".py"):
                ast.parse(files[name], filename=name)
        local_modules = {_module_name(name) for name in files if name.endswith(".py")}
        declared_modules = {_module_name(name) for name in declared if name.endswith(".py")}
        for name in declared:
            if not name.endswith(".py"):
                continue
            tree = ast.parse(files[name], filename=name)
            imported = _local_imports(tree, name)
            if (imported & local_modules) - declared_modules:
                raise ValueError("undeclared local strategy source")
        identity = str(uuid4())
        target, hashes = publish_files(self.root / "strategies", identity, {name: files[name] for name in declared})
        content_hash = digest(json_bytes(hashes))
        with self.transaction() as db:
            db.execute("INSERT INTO strategies VALUES (?, ?, ?, ?)", (identity, str(target), content_hash, json_bytes(manifest).decode()))
        return StrategyRevision(identity, target, content_hash, manifest)

    def save_experiment(self, experiment_id: str | None, expected_revision: int | None, payload: dict) -> ExperimentDraft:
        normalized = json.loads(json_bytes(payload))
        if "strategy_revision_id" in normalized and "strategy_params" in normalized:
            strategy = self.get_strategy(normalized["strategy_revision_id"])
            normalized["effective_strategy_params"] = validate_parameters(strategy.manifest["parameter_schema"], normalized["strategy_params"])
        encoded = json_bytes(normalized).decode()
        with self.transaction() as db:
            if experiment_id is None:
                if expected_revision is not None:
                    raise ValueError("new experiment cannot have expected revision")
                experiment_id = str(uuid4())
                revision = 1
                db.execute("INSERT INTO experiments VALUES (?, ?, ?)", (experiment_id, revision, encoded))
            else:
                if expected_revision is None:
                    raise ValueError("expected revision required")
                updated = db.execute("UPDATE experiments SET revision=revision+1, payload_json=? WHERE id=? AND revision=?", (encoded, experiment_id, expected_revision))
                if updated.rowcount != 1:
                    raise ValueError("experiment revision conflict")
                revision = expected_revision + 1
        return ExperimentDraft(experiment_id, revision, json.loads(encoded))

    def get_experiment(self, identity: str, db=None) -> ExperimentDraft:
        if db is None:
            with self.transaction() as connection:
                return self.get_experiment(identity, connection)
        row = db.execute("SELECT * FROM experiments WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return ExperimentDraft(row["id"], row["revision"], json.loads(row["payload_json"]))

    def get_dataset(self, identity: str, db=None) -> DatasetVersion:
        if db is None:
            with self.transaction() as connection:
                return self.get_dataset(identity, connection)
        row = db.execute("SELECT * FROM datasets WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return DatasetVersion(row["id"], Path(row["path"]), row["content_hash"], json.loads(row["manifest_json"]))

    def get_strategy(self, identity: str, db=None) -> StrategyRevision:
        if db is None:
            with self.transaction() as connection:
                return self.get_strategy(identity, connection)
        row = db.execute("SELECT * FROM strategies WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return StrategyRevision(row["id"], Path(row["path"]), row["content_hash"], json.loads(row["manifest_json"]))

    def get_run(self, identity: str, db=None) -> RunRecord:
        if db is None:
            with self.transaction() as connection:
                return self.get_run(identity, connection)
        row = db.execute("SELECT runs.*, jobs.state, jobs.completed_through, jobs.error, jobs.created_at, jobs.started_at, jobs.finished_at FROM runs JOIN jobs ON jobs.id=runs.job_id WHERE runs.run_id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return RunRecord(row["run_id"], row["job_id"], Path(row["manifest_path"]), row["manifest_hash"], row["source"], row["parent_run_id"], row["group_id"], row["experiment_id"], row["experiment_revision"], row["state"], row["completed_through"], row["error"], row["created_at"], row["started_at"], row["finished_at"])

    def list_jobs(self) -> list[JobRecord]:
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY enqueue_seq").fetchall()
        return [self._job(row) for row in rows]

    @staticmethod
    def _job(row) -> JobRecord:
        values = dict(row)
        values["artifact_root"] = Path(values["artifact_root"])
        return JobRecord(**values)

    def get_job(self, identity: str) -> JobRecord:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise KeyError(identity)
            return self._job(row)

    def claim_job(self) -> JobRecord | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY enqueue_seq LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running', started_at=? WHERE id=? AND state='queued'", (utc_now(), row["id"]))
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def request_cancel(self, identity: str) -> JobRecord:
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state=CASE state WHEN 'queued' THEN 'cancelled' ELSE 'cancel_requested' END, finished_at=CASE WHEN state='queued' THEN ? ELSE finished_at END WHERE id=? AND state IN ('queued','running')", (utc_now(), identity))
        return self.get_job(identity)

    def register_process(self, identity: str, pid: int, process_start: str) -> bool:
        with self.transaction() as db:
            return db.execute("UPDATE jobs SET pid=?, process_start=? WHERE id=? AND state IN ('running','cancel_requested') AND pid IS NULL", (pid, process_start, identity)).rowcount == 1

    def checkpoint(self, identity: str, heartbeat: str | None, completed_through: str | None) -> None:
        with self.transaction() as db:
            db.execute("UPDATE jobs SET heartbeat=COALESCE(?,heartbeat), completed_through=COALESCE(?,completed_through) WHERE id=? AND state IN ('running','cancel_requested')", (heartbeat, completed_through, identity))

    def finish_job(self, identity: str, state: str, *, expected: str, error: str | None = None) -> bool:
        allowed = {"running": {"succeeded", "failed", "interrupted"}, "cancel_requested": {"cancelled", "interrupted"}}
        if state not in allowed.get(expected, set()):
            raise ValueError("invalid terminal transition")
        with self.transaction() as db:
            return db.execute("UPDATE jobs SET state=?, error=?, finished_at=? WHERE id=? AND state=?", (state, error, utc_now(), identity, expected)).rowcount == 1

    def finish_data_job(self, identity: str, bundle_path: Path, result: dict) -> bool:
        """Publish a verified data version and success in one conditional commit."""
        job = self.get_job(identity)
        if job.state != "running":
            return False
        manifest, hashes, market_ref = None, None, None
        if result.get("state") == "complete":
            bundle = load_dataset(bundle_path)
            manifest = bundle.manifest.model_dump(mode="json")
            hashes = {name: digest(content) for name, content in source_files(bundle_path).items()}
            if job.kind == "fetch":
                from quanto.workbench.data_jobs import validate_market_result
                frozen = json.loads(Path(job.input_path).read_text(encoding="utf-8"))
                market_ref = validate_market_result(frozen["input"], result, bundle)
        elif result.get("state") != "incomplete":
            raise ValueError("invalid data result state")
        with self.transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None or row["state"] != "running":
                return False
            if manifest is not None:
                db.execute("INSERT INTO datasets VALUES (?,?,?,?)", (identity, str(bundle_path), digest(json_bytes(hashes)), json_bytes(manifest).decode()))
                if market_ref is not None:
                    db.execute("INSERT INTO dataset_market_sources VALUES (?,?,?)", (identity, market_ref.id, market_ref.manifest_digest))
            db.execute("UPDATE jobs SET state='succeeded', finished_at=? WHERE id=? AND state='running'", (utc_now(), identity))
            return True

    def dataset_market_source(self, identity: str, db=None) -> dict | None:
        if db is None:
            with self.transaction() as connection:
                return self.dataset_market_source(identity, connection)
        row = db.execute("SELECT snapshot_id, manifest_digest FROM dataset_market_sources WHERE dataset_version_id=?", (identity,)).fetchone()
        return {"id": row["snapshot_id"], "manifest_digest": row["manifest_digest"]} if row else None
