"""Frozen child entry point; the bootstrap intentionally imports only stdlib."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import random
import re
import sqlite3
from contextlib import closing
import sys
import threading
import time
import traceback
import tomllib


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.005)


def read_json(path: Path):
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.005)


def process_identity(pid: int) -> str | None:
    import psutil
    try:
        return str(psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        return None


def same_process(pid: int, identity: str) -> bool:
    return process_identity(pid) == identity


def wait_for_ack(control_dir: Path, descriptor: dict, *, clock=time.monotonic,
                 sleep=time.sleep, alive=same_process) -> bool:
    deadline = clock() + descriptor["handshake_timeout"]
    while clock() < deadline:
        if not alive(descriptor["parent_pid"], descriptor["parent_start"]):
            return False
        ack = control_dir / "ack.json"
        if ack.exists() and read_json(ack) == {"job_id": descriptor["job_id"]}:
            return True
        sleep(0.02)
    return False


class EventWriter:
    """One sequence for all phases; complete days are committed as delta rows."""
    def __init__(self, control_dir: Path):
        self.control_dir = control_dir
        self.seq = 0
        temporary = control_dir / "partial.init.sqlite3"
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rows (table_name TEXT, row_key TEXT, payload TEXT, PRIMARY KEY(table_name,row_key));
                CREATE TABLE IF NOT EXISTS progress (singleton INTEGER PRIMARY KEY CHECK(singleton=1), completed_through TEXT);
            """)
            db.commit()
        temporary.rename(control_dir / "partial.sqlite3")

    def emit(self, event) -> None:
        value = asdict(event) if not isinstance(event, dict) else dict(event)
        self.seq += 1
        value["seq"] = self.seq
        payload = value.get("payload", {})
        if "tables" in payload:
            with closing(sqlite3.connect(self.control_dir / "partial.sqlite3")) as db, db:
                for name, rows in payload["tables"].items():
                    for index, row in enumerate(rows):
                        key = str(row["id"]) if name == "orders" else f"{self.seq}:{index}"
                        db.execute("INSERT INTO rows VALUES (?,?,?) ON CONFLICT(table_name,row_key) DO UPDATE SET payload=excluded.payload",
                                   (name, key, json.dumps(row, ensure_ascii=False, allow_nan=False)))
                db.execute("INSERT OR REPLACE INTO progress VALUES (1,?)", (payload["completed_through"],))
        with (self.control_dir / "events.ndjson").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if "completed_through" in payload:
            atomic_json(self.control_dir / "progress.json", {"completed_through": payload["completed_through"]})

    def message(self, code: str, message: str, level="info") -> None:
        self.emit({"phase": "worker", "asof": None, "completed_days": 0, "total_days": 0,
                   "level": level, "code": code, "message": message, "payload": {}})


def read_partial(control_dir: Path) -> dict:
    from quanto.backtest.models import TABLE_COLUMNS
    result = {"completed_through": None, "tables": {name: [] for name in TABLE_COLUMNS}}
    path = control_dir / "partial.sqlite3"
    if not path.exists():
        return result
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        progress = db.execute("SELECT completed_through FROM progress WHERE singleton=1").fetchone()
        result["completed_through"] = progress[0] if progress else None
        for name, payload in db.execute("SELECT table_name,payload FROM rows ORDER BY rowid"):
            result["tables"][name].append(json.loads(payload))
    return result


def read_completed_through(control_dir: Path) -> str | None:
    path = control_dir / "partial.sqlite3"
    if not path.exists():
        return None
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT completed_through FROM progress WHERE singleton=1").fetchone()
        return row[0] if row else None


def verify_bootstrap(descriptor: dict) -> dict:
    path = Path(descriptor["manifest_path"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != descriptor["manifest_hash"]:
        raise ValueError("SNAPSHOT_INVALID: 清单哈希无效")
    manifest = read_json(path)
    name = "src/quanto/workbench/worker.py"
    if hashlib.sha256((path.parent / name).read_bytes()).hexdigest() != manifest["files"][name]:
        raise ValueError("SNAPSHOT_INVALID: 引导文件哈希无效")
    return manifest


def _hash_tree(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("SNAPSHOT_INVALID: referenced directory missing or symbolic")
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("SNAPSHOT_INVALID: symbolic frozen input")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _verify_ref(ref: dict, expected_root: Path | None = None) -> None:
    if not isinstance(ref, dict) or not {"id", "path", "content_hash", "files"} <= set(ref):
        raise ValueError("SNAPSHOT_INVALID: frozen reference invalid")
    path = Path(ref["path"])
    if expected_root is not None and path != expected_root:
        raise ValueError("SNAPSHOT_INVALID: frozen reference path mismatch")
    files = ref["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("SNAPSHOT_INVALID: frozen reference file list invalid")
    for name in files:
        parts = PurePosixPath(name)
        if not isinstance(name, str) or not name or "\\" in name or ":" in name or parts.is_absolute() or any(part in {".", ".."} for part in name.split("/")):
            raise ValueError("SNAPSHOT_INVALID: unsafe frozen reference path")
    if _hash_tree(path) != files:
        raise ValueError("SNAPSHOT_INVALID: referenced artifact changed")
    content = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if content != ref["content_hash"]:
        raise ValueError("SNAPSHOT_INVALID: referenced artifact hash mismatch")


def _verify_environment(manifest: dict, root: Path) -> None:
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    metadata = manifest["environment"]
    actual = {"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
              "declared_requirements": declared.get("dependencies", []),
              "declared_optional_requirements": declared.get("optional-dependencies", {}),
              "installed_distributions": [list(item) for item in sorted({
                  (re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower(), dist.version)
                  for dist in importlib.metadata.distributions()})],
              "lock_hashes": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                              for name in metadata["lock_hashes"]}}
    if actual != metadata:
        raise ValueError("ENVIRONMENT_CHANGED: execution environment differs from snapshot")


def _verify_inputs(descriptor: dict) -> dict:
    manifest = verify_bootstrap(descriptor)
    if manifest.get("schema_version") != 3 or manifest.get("framework_contract_version") != "multi-asset-v1":
        raise ValueError("SNAPSHOT_INVALID: framework contract missing")
    root = Path(descriptor["manifest_path"]).parent
    actual = _hash_tree(root)
    actual.pop("manifest.json", None)
    if set(actual) != set(manifest["files"]):
        raise ValueError("SNAPSHOT_INVALID: 冻结文件集合无效")
    if actual != manifest["files"]:
        raise ValueError("SNAPSHOT_INVALID: 冻结文件哈希无效")
    ref = manifest["dataset_ref"]
    if ref["id"] != manifest["dataset_version_id"] or ref["content_hash"] != manifest["dataset_content_hash"]:
        raise ValueError("SNAPSHOT_INVALID: dataset reference mismatch")
    _verify_ref(ref, root.parent.parent / "dataset_objects" / ref["content_hash"])
    _verify_ref(manifest["strategy_ref"], root / "strategy")
    if manifest["strategy_ref"]["id"] != manifest["strategy_revision_id"]:
        raise ValueError("SNAPSHOT_INVALID: strategy reference mismatch")
    _verify_environment(manifest, root)
    return manifest


def verify_source_hashes(descriptor: dict, *, check_environment: bool = False) -> None:
    root = Path(descriptor["source_root"])
    expected = descriptor["source_hashes"]
    actual = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.rglob("*") if path.is_file()}
    actual.pop("input.json", None)
    if any(path.is_symlink() for path in root.rglob("*")) or actual != expected:
        raise ValueError("SNAPSHOT_INVALID: 数据任务源码或上传快照无效")
    payload = descriptor.get("input", {})
    for name in ("dataset_ref", "training_dataset_ref", "policy_ref", "policy_resume_ref"):
        if name in payload:
            _verify_ref(payload[name])
    expected_environment = payload.get("environment")
    if check_environment and descriptor["kind"] in {"train_policy", "policy_backtest"}:
        if "execution_environment" not in payload:
            raise ValueError("SNAPSHOT_INVALID: complete execution environment missing")
        _verify_environment({"environment": payload["execution_environment"]}, root)
        from importlib.metadata import PackageNotFoundError, version
        current = {"python": platform.python_version()}
        for name in ("numpy", "pandas", "pyarrow", "torch", "stable-baselines3", "gymnasium", "cloudpickle"):
            try:
                current[name] = version(name)
            except PackageNotFoundError:
                current[name] = None
        if current != expected_environment:
            raise ValueError("ENVIRONMENT_CHANGED: learning dependencies differ from frozen input")


def validate_backtest_output(directory: Path, hashes: dict[str, str]) -> None:
    import csv
    from quanto.backtest.models import TABLE_COLUMNS
    from quanto.reports.backtest_report import OUTPUT_FILES, REPORT_TABLE_COLUMNS
    if set(hashes) != set(OUTPUT_FILES) or {p.name for p in directory.iterdir()} != set(OUTPUT_FILES):
        raise ValueError("INVALID_OUTPUT: 产物文件集合不完整")
    for name, expected in hashes.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError("INVALID_OUTPUT: 产物哈希不一致")
    manifest = read_json(directory / "manifest.json")
    version = manifest.get("schema_version")
    if version not in {"1", "2", "3", "4", "5"}:
        raise ValueError("INVALID_OUTPUT: 不支持的报告结构版本")
    if manifest != {"schema_version": version, "files": {name: value for name, value in hashes.items() if name != "manifest.json"}}:
        raise ValueError("INVALID_OUTPUT: 产物清单不一致")
    # Historical schema 1 stored the approved target in signals; never invent its original intent.
    table_columns = dict(TABLE_COLUMNS)
    if version in {"1", "2", "3"}:
        table_columns["fills"] = [name for name in table_columns["fills"] if name not in {"stamp_duty", "transfer_fee"}]
    if version == "1":
        table_columns["signals"] = ["date", "intent", "weights", "reasons", "diagnostics"]
    columns = {**table_columns, **REPORT_TABLE_COLUMNS}
    if version in {"3", "5"}:
        columns = {name: ([*expected[:expected.index("symbol") + 1], "instrument_id", *expected[expected.index("symbol") + 1:]]
                  if "symbol" in expected else expected) for name, expected in columns.items()}
        table_columns = {name: columns[name] for name in table_columns}
    for name, expected in columns.items():
        with (directory / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            if next(reader, None) != list(expected) or any(len(row) != len(expected) for row in reader):
                raise ValueError("INVALID_OUTPUT: 产物表结构无效")
    def reject_constant(value):
        raise ValueError("INVALID_OUTPUT: 产物数值不是有限数")
    metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"), parse_constant=reject_constant)
    metadata = read_json(directory / "metadata.json")
    if metrics.get("schema_version") != version or metrics.get("artifacts") != list(OUTPUT_FILES) or not isinstance(metrics.get("metrics"), dict):
        raise ValueError("INVALID_OUTPUT: 指标结构无效")
    if metadata.get("table_columns") != table_columns or metadata.get("schema_version") != version:
        raise ValueError("INVALID_OUTPUT: 元数据结构无效")
    if version in {"2", "3", "4", "5"} and (type(metadata.get("is_demo")) is not bool or not isinstance(metadata.get("source"), str)):
        raise ValueError("INVALID_OUTPUT: 数据来源元数据无效")
    if version in {"3", "5"}:
        from quanto.framework import ResolvedConfiguration
        framework = ResolvedConfiguration.model_validate(metadata.get("framework"))
        if framework.operation != "fixed_backtest" or not isinstance(metadata.get("execution_provenance"), dict):
            raise ValueError("INVALID_OUTPUT: framework provenance missing")


def run_frozen_backtest(descriptor: dict, control_dir: Path, emit, cancelled) -> dict:
    # Heartbeat and cancellation supervision already run before this potentially
    # large scan. Verify every source byte before importing project execution code.
    manifest = _verify_inputs(descriptor)
    from quanto.workbench.snapshots import verify_framework_snapshot
    resolved = verify_framework_snapshot(Path(descriptor["manifest_path"]).parent)
    if resolved.request_hash != manifest["resolved_configuration"]["request_hash"]:
        raise ValueError("SNAPSHOT_INVALID: resolved framework request changed")
    import numpy as np
    from quanto.backtest import engine
    from quanto.config import BacktestSpec
    from quanto.backtest.dataset_loaders import load_dataset, load_dataset_for_run
    from quanto.reports.backtest_report import build_report, write_backtest_outputs
    from quanto.strategies.base import validate_warmup_window
    from quanto.workbench.definitions import DefinitionRegistry
    from quanto.data.point_in_time import TableSemantics

    manifest_path = Path(descriptor["manifest_path"])
    source_root = manifest_path.parent
    registry = DefinitionRegistry()
    definitions = {item.ref.kind: registry.get(item.ref) for item in resolved.definitions}
    table_semantics = tuple(TableSemantics.from_binding(item) for item in resolved.asset_binding.table_semantics)
    feature = next((item for item in resolved.definitions if item.ref.kind == "feature"), None)
    random.seed(manifest["seed"])
    np.random.seed(manifest["seed"] % 2**32)
    sys.path.insert(1, str(source_root / "strategy"))
    entry = manifest["strategy_manifest"]["entrypoint"].partition(":")[0]
    module = importlib.import_module(".".join(Path(entry).with_suffix("").parts))
    strategy = module.create_strategy(manifest["effective_strategy_params"])
    if type(getattr(strategy, "warmup_bars", None)) is not int or strategy.warmup_bars < 0 or not all(callable(getattr(strategy, name, None)) for name in ("should_evaluate", "on_close")):
        raise ValueError("INVALID_STRATEGY: 策略协议无效")
    spec = BacktestSpec.model_validate(manifest["backtest_spec"])
    dataset_path = Path(manifest["dataset_ref"]["path"])
    bundle = load_dataset_for_run(dataset_path, spec)
    spec.validate_for_dataset(bundle)
    missing = set(manifest["strategy_manifest"].get("required_data_columns", [])) - set(bundle.prices.columns)
    if missing:
        raise ValueError("INVALID_STRATEGY: 数据缺少策略必需列")
    validate_warmup_window(strategy, bundle.prices.loc[bundle.prices.symbol.isin(spec.symbols), "date"], spec.start, spec.end)
    atomic_json(control_dir / "runtime.json", {"warmup_bars": strategy.warmup_bars, "engine_file": engine.__file__,
        "strategy_file": module.__file__, "reproducibility": manifest["reproducibility"], "environment_verified": True,
        "seed": manifest["seed"], "numpy_hash_seed": manifest["seed"] % 2**32})
    result = engine.run_backtest(bundle, strategy, spec, emit, cancelled,
        trading_definition=definitions["trading"], asset_binding=resolved.asset_binding,
        feature_definition=definitions.get("feature"),
        feature_params=feature.effective_params if feature is not None else None,
        table_semantics=table_semantics)
    report = build_report(result, bundle, spec)
    report.metadata.update({name: manifest[name] for name in ("dataset_version_id", "strategy_revision_id")})
    from quanto.framework import canonical_hash
    report.metadata["framework"] = manifest["resolved_configuration"]
    report.metadata["execution_provenance"] = {
        "source_hashes": manifest["files"], "environment_fingerprint": canonical_hash(manifest["environment"]),
        "model_id": None, "policy_version_id": None, "training_boundaries": None,
        "basket_selection_note": resolved.basket.selection_note,
        "best_checkpoint": None, "last_checkpoint": None,
        "feature_transform": None, "training_updates": None,
    }
    if resolved.basket.selection_origin is not None:
        report.metadata["screening_record"] = manifest["screening_record"]
        report.metadata["execution_provenance"]["selection_origin"] = resolved.basket.selection_origin.model_dump(mode="json")
        report.metadata["execution_provenance"]["selection_evaluation"] = "development_research_not_historical_selection_validation"
        report.warnings.append(f"Screening as of {resolved.basket.selection_origin.as_of.isoformat()}; "
                               "this development backtest does not validate historical stock selection.")
    stage = control_dir.parent / "staging"
    hashes = write_backtest_outputs(stage, result, report)
    validate_backtest_output(stage, hashes)
    for path in stage.iterdir():
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    if cancelled():
        from quanto.backtest.models import BacktestCancelled
        raise BacktestCancelled()
    stage.rename(control_dir.parent / "final")
    return {"hashes": hashes}


def run_data_job(descriptor: dict, control_dir: Path, emit, cancelled) -> dict:
    if descriptor["kind"] == "fetch":
        from quanto.workbench.configuration import FrameworkResolver
        from quanto.workbench.framework_requests import FetchRequest
        from quanto.framework import CapabilityReport
        framework = descriptor["input"]["framework"]
        request = FetchRequest.model_validate(framework["request"])
        frozen_report = CapabilityReport.model_validate(framework["capability_report"])
        checked = FrameworkResolver(None).check(request)
        if (checked.status != "ready" or checked.model_dump(mode="json", exclude={"generated_at"}) !=
                frozen_report.model_dump(mode="json", exclude={"generated_at"})):
            raise ValueError("SNAPSHOT_INVALID: fetch capability changed from frozen definition")
    from quanto.workbench.data_jobs import execute_data_job, execute_strategy_validation
    from quanto.workbench.revisions import source_files
    stage = control_dir.parent / "staging"
    stage.mkdir()
    payload = dict(descriptor["input"])
    payload.pop("framework", None)
    payload["_output_dir"] = str(stage)
    payload["_source_root"] = descriptor["source_root"]
    if descriptor["kind"] == "validate_strategy":
        result = execute_strategy_validation(payload, cancelled)
    else:
        result = execute_data_job(descriptor["kind"], payload, emit, cancelled)
    atomic_json(stage / "result.json", result)
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in source_files(stage).items()}
    if cancelled():
        raise RuntimeError("CANCELLED: 任务已取消")
    stage.rename(control_dir.parent / "final")
    return {"hashes": hashes, **result}


def run_learning_job(descriptor: dict, control_dir: Path, emit, cancelled) -> dict:
    if descriptor["kind"] in {"train_policy", "policy_backtest"}:
        from quanto.framework import ModelFrameworkBinding, ResolvedConfiguration
        from quanto.workbench.frozen_configuration import verify_frozen_configuration
        payload = descriptor["input"]
        resolved = ResolvedConfiguration.model_validate(payload["resolved_configuration"])
        binding = ModelFrameworkBinding.model_validate(payload["model_binding"])
        verify_frozen_configuration(resolved, dataset_ref=payload["dataset_ref"],
            model_binding=binding, policy_ref=payload.get("policy_ref") or payload.get("policy_resume_ref"),
            policy_context=payload.get("policy_context") or payload.get("policy_resume_context"),
            training_dataset_ref=payload.get("training_dataset_ref"), screening_record=payload.get("screening_record"))
    from quanto.learning.jobs import execute_learning_job
    return execute_learning_job(descriptor, control_dir, emit, cancelled)


JOB_HANDLERS = {"backtest": run_frozen_backtest,
                "demo": run_data_job, "fetch": run_data_job,
                "import": run_data_job, "validate_strategy": run_data_job,
                "prepare_learning": run_learning_job, "train": run_learning_job,
                "model_backtest": run_learning_job, "train_policy": run_learning_job,
                "policy_backtest": run_learning_job}


def validate_job_output(kind: str, root: Path, receipt: dict) -> None:
    if kind in {"prepare_learning", "train", "model_backtest", "train_policy", "policy_backtest"}:
        from quanto.learning.jobs import validate_learning_output
        validate_learning_output(kind, root / "final", receipt["result"])
    elif kind == "backtest":
        validate_backtest_output(root / "final", receipt["result"]["hashes"])
    else:
        if kind not in JOB_HANDLERS:
            raise ValueError("UNSUPPORTED_JOB: 未注册的任务类型")
        from quanto.workbench.revisions import safe_name, source_files
        final = root / "final"
        hashes = receipt["result"]["hashes"]
        if not final.is_dir() or set(source_files(final)) != set(hashes):
            raise ValueError("INVALID_OUTPUT: 数据任务文件集合无效")
        for name, expected in hashes.items():
            safe_name(name)
            if not (final / name).is_file() or hashlib.sha256((final / name).read_bytes()).hexdigest() != expected:
                raise ValueError("INVALID_OUTPUT: 数据任务哈希无效")
        result = read_json(final / "result.json")
        if {key: value for key, value in receipt["result"].items() if key != "hashes"} != result:
            raise ValueError("INVALID_OUTPUT: receipt differs from hashed result")
        if result.get("state") == "complete" and kind in ("demo", "fetch", "import"):
            from quanto.backtest.dataset_loaders import load_dataset
            bundle = load_dataset(final)
            if kind == "fetch":
                from quanto.workbench.data_jobs import validate_market_result
                frozen = read_json(root / "control" / "job.json")
                validate_market_result(frozen["input"], result, bundle)
        elif result.get("state") not in ("incomplete", None) and kind != "validate_strategy":
            raise ValueError("INVALID_OUTPUT: 数据任务状态无效")


def worker_main(job_path: Path, control_dir: Path) -> int:
    descriptor = read_json(job_path)
    identity = process_identity(os.getpid())
    atomic_json(control_dir / "identity.json", {"job_id": descriptor["job_id"], "pid": os.getpid(), "process_start": identity})
    stopped = threading.Event()
    def watchdog():
        while not stopped.is_set():
            if not same_process(descriptor["parent_pid"], descriptor["parent_start"]):
                os._exit(75)
            try:
                atomic_json(control_dir / "heartbeat.json", {"job_id": descriptor["job_id"], "monotonic": time.monotonic(), "at": datetime.now(timezone.utc).isoformat()})
            except PermissionError:
                # Windows locks can outlast atomic_json's short retries. Retry next
                # interval; the scheduler still times out a persistently stale beat.
                pass
            stopped.wait(descriptor["heartbeat_interval"])
    thread = threading.Thread(target=watchdog, daemon=True)
    thread.start()
    try:
        if not wait_for_ack(control_dir, descriptor):
            return 75
        events = EventWriter(control_dir)
        events.message("WORKER_STARTED", "工作进程已启动")
        try:
            handler = JOB_HANDLERS.get(descriptor["kind"])
            if handler is None:
                raise ValueError("UNSUPPORTED_JOB: 未注册的任务类型")
            result = handler(descriptor, control_dir, events.emit, lambda: (control_dir / "cancel").exists())
            events.message("WORKER_COMPLETED", "任务计算和产物校验已完成")
            atomic_json(control_dir / "receipt.json", {"job_id": descriptor["job_id"], "state": "succeeded", "result": result})
            return 0
        except BaseException as exc:
            models = sys.modules.get("quanto.backtest.models")
            cancelled = models is not None and isinstance(exc, models.BacktestCancelled)
            code = "CANCELLED" if cancelled else "WORKER_FAILED"
            message = "任务已响应取消" if cancelled else "任务执行失败，请查看诊断日志"
            (control_dir / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
            events.message(code, message, "warning" if cancelled else "error")
            atomic_json(control_dir / "receipt.json", {"job_id": descriptor["job_id"], "state": "cancelled" if cancelled else "failed", "error": f"{code}: {message}"})
            return 2 if cancelled else 1
    finally:
        stopped.set()
        thread.join()


def _bootstrap(job_path: Path, control_dir: Path) -> int:
    descriptor = read_json(job_path)
    root = Path(descriptor["source_root"])
    # Metadata discovery needs the interpreter's installed distribution paths.
    # No project source is importable until its complete byte set is verified.
    sys.path[:] = [*descriptor["site_paths"], *sys.path]
    try:
        if descriptor["kind"] == "backtest":
            _verify_inputs(descriptor)
        else:
            verify_source_hashes(descriptor, check_environment=True)
    except BaseException:
        (control_dir / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
        atomic_json(control_dir / "receipt.json", {"job_id": descriptor["job_id"], "state": "failed",
            "error": "SNAPSHOT_INVALID: frozen inputs failed bootstrap verification"})
        return 1
    # -P -S plus a sanitized environment excludes cwd, PYTHONPATH and site hooks.
    # Restore only the interpreter's dependency directories, then frozen sources.
    sys.path.insert(0, str(root / "src"))
    sys.dont_write_bytecode = True
    return worker_main(job_path, control_dir)


if __name__ == "__main__":
    raise SystemExit(_bootstrap(Path(sys.argv[1]), Path(sys.argv[2])))
