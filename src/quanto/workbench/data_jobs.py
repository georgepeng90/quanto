from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

import pandas as pd

from quanto.data import MarketData
from quanto.data.contracts import MarketDataError, SnapshotRef
from quanto.backtest.dataset_identity import (EVIDENCE_FILE, ImportIdentityEvidence, canonicalize_import, declared_dataset_files,
                                         file_digests, make_import_evidence, publish_import_evidence, standard_table_semantics)
from quanto.backtest.market_dataset import build_data_requirement, assemble_dataset
from quanto.backtest.dataset_loaders import load_dataset
from quanto.backtest.dataset_storage import write_parquet_dataset
from quanto.backtest.dataset_schemas import DatasetManifest
from quanto.config import validate_parameters
from quanto.sample_data import generate_sample_data
from quanto.workbench.store import WorkbenchStore
from quanto.workbench.revisions import digest, json_bytes, safe_name, source_files


def _emit(emit, code: str, message: str, payload: dict | None = None) -> None:
    emit({"phase": "data", "asof": None, "completed_days": 0, "total_days": 0,
          "level": "info", "code": code, "message": message, "payload": payload or {}})


def _publish_import(upload: Path, destination: Path) -> None:
    if upload.suffix != ".zip":
        raise ValueError("IMPORT_FORMAT: 请上传包含数据集文件的 ZIP")
    with zipfile.ZipFile(upload) as archive:
        members = archive.infolist()
        if len(members) > 100 or sum(item.file_size for item in members) > 300_000_000:
            raise ValueError("IMPORT_SIZE: 导入文件过大")
        names = {item.filename for item in members}
        if len(names) != len(members):
            raise ValueError("IMPORT_FILES: 导入包含重复文件")
        if "manifest.json" not in names:
            raise ValueError("IMPORT_MANIFEST: 缺少数据集清单")
        manifest = DatasetManifest.model_validate(json.loads(archive.read("manifest.json")))
        allowed = {"manifest.json", EVIDENCE_FILE, *manifest.auxiliary_tables.values()}
        for table in ("prices", "benchmark", "calendar", "instruments", "status", "events"):
            matches = names & {f"{table}.csv", f"{table}.parquet"}
            if len(matches) != 1:
                raise ValueError("IMPORT_TABLES: 数据表必须且只能提供一种格式")
            allowed.update(matches)
        if names - allowed:
            raise ValueError("IMPORT_FILES: 导入包含未声明或缺失文件")
        if EVIDENCE_FILE not in names:
            raise ValueError("IDENTITY_UNRESOLVED: 导入缺少 identity_evidence.json 身份资料")
        for item in members:
            safe_name(item.filename)
            if item.is_dir():
                raise ValueError("IMPORT_PATH: 数据集只能包含根目录常规文件")
            path = destination / item.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(item))


def validate_market_result(payload: dict, result: dict, bundle=None) -> SnapshotRef:
    if result.get("market_prefetch_state") != "ready" or not result.get("market_snapshot_ref"):
        raise ValueError("INVALID_OUTPUT: market snapshot is not ready")
    claimed = SnapshotRef.model_validate(result["market_snapshot_ref"])
    try:
        with MarketData(Path(payload["market_root"]), providers=()) as market:
            ref = market.resolve_snapshot(claimed.id)
    except MarketDataError as exc:
        raise ValueError(f"INVALID_OUTPUT: {exc}") from exc
    if ref.manifest_digest != claimed.manifest_digest or ref.quality["requirement_key"] != build_data_requirement(payload).key:
        raise ValueError("INVALID_OUTPUT: market snapshot differs from frozen requirement")
    if bundle is not None and bundle.manifest.source != f"market snapshot {ref.id}":
        raise ValueError("INVALID_OUTPUT: assembled dataset differs from market source")
    return ref


def _fetch_dataset(payload, destination, emit, cancelled):
    requirement = build_data_requirement(payload)
    with MarketData(Path(payload["market_root"])) as market:
        task = market.prefetch(requirement, refresh=payload.get("refresh", False))
        while True:
            if cancelled():
                market.cancel_prefetch(task.id)
                raise RuntimeError("CANCELLED: 数据任务已取消")
            try:
                ref = market.wait_ready(task.id, timeout=.2)
                break
            except MarketDataError as exc:
                status = market.get_prefetch(task.id)
                if exc.code == "WAIT_TIMEOUT":
                    _emit(emit, "MARKET_PREPARING", "行情预缓存准备中", status.model_dump(mode="json"))
                    continue
                if status.state == "cancelled":
                    raise RuntimeError("CANCELLED: 数据任务已取消") from exc
                _emit(emit, "MARKET_NOT_READY", "行情预缓存未就绪", status.model_dump(mode="json"))
                return {"state": "incomplete", "dataset_version_id": None, "market_prefetch_state": status.state,
                    "market_snapshot_ref": None, "coverage": status.coverage.model_dump(mode="json"),
                    "limitations": list(status.limitations), "reason": status.reason}
        _emit(emit, "MARKET_READY", "行情快照已就绪", {"snapshot_id": ref.id})
        assumptions = {**payload.get("research_assumptions", {}), "price_mode": payload.get("price_mode", "adjusted_research")}
        bundle = assemble_dataset(market, ref, requirement, assumptions)
        write_parquet_dataset(bundle, destination)
        return {"market_prefetch_state": "ready", "market_snapshot_ref": ref.model_dump(mode="json")}


def execute_data_job(kind: str, payload: dict, emit, cancelled) -> dict:
    """Worker-only data operation. Registration is done by the scheduler after exit."""
    if cancelled():
        raise RuntimeError("CANCELLED: 数据任务已取消")
    _emit(emit, "DATA_JOB_STARTED", "数据任务已开始")
    market_result = {}
    destination = Path(payload.get("_output_dir") or tempfile.mkdtemp(prefix="quanto-data-"))
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "demo":
        generate_sample_data(destination, years=int(payload.get("years", 1)), seed=int(payload.get("seed", 7)))
    elif kind == "import":
        upload_id = payload.get("upload_id", "")
        from uuid import UUID
        UUID(upload_id)
        frozen = Path(payload["_upload_path"])
        if not frozen.is_file() or frozen.parent != Path(payload["_source_root"]):
            raise ValueError("UPLOAD_NOT_FOUND: 上传文件不存在")
        _publish_import(frozen, destination)
    elif kind == "fetch":
        market_result = _fetch_dataset(payload, destination, emit, cancelled)
        if market_result.get("state") == "incomplete":
            return market_result
    else:
        raise ValueError("UNSUPPORTED_JOB: 未注册的数据任务")
    if cancelled():
        raise RuntimeError("CANCELLED: 数据任务已取消")
    bundle = load_dataset(destination)
    evidence = None
    if kind == "demo":
        from quanto.data.contracts import InstrumentId
        from quanto.framework import AssetMapping, BenchmarkBinding
        from quanto.sample_data import SAMPLE_INSTRUMENTS
        ref = "synthetic:sample-data-v1"
        evidence = ImportIdentityEvidence.model_validate(make_import_evidence(destination,
            assets=tuple(AssetMapping(instrument_id=InstrumentId(exchange=exchange, instrument_type="etf", code=code), local_symbol=code, evidence_ref=ref) for exchange, code in SAMPLE_INSTRUMENTS),
            benchmark=BenchmarkBinding(custom_series_id="quanto-synthetic-benchmark-v1", return_basis=bundle.manifest.benchmark_type, evidence_ref=ref),
            source_document="Synthetic data generated by quanto.sample_data; identities are explicit example labels, not observed market data.",
            table_semantics=standard_table_semantics(bundle, ref)))
    elif kind == "import":
        evidence = ImportIdentityEvidence.model_validate_json((destination / EVIDENCE_FILE).read_bytes())
        if dict(evidence.input_files) != file_digests(declared_dataset_files(destination)):
            raise ValueError("IDENTITY_UNRESOLVED: import evidence input file hash mismatch")
    if evidence is not None:
        bundle = canonicalize_import(bundle, evidence)
    # Only this job's unpublished staging files are replaced; imports and old versions stay intact.
    old_names = {"manifest.json", *bundle.manifest.auxiliary_tables.values()}
    old_names.update(f"{name}.{suffix}" for name in ("prices", "benchmark", "calendar", "instruments", "status", "events")
                     for suffix in ("csv", "parquet") if (destination / f"{name}.{suffix}").exists())
    if (destination / EVIDENCE_FILE).exists():
        old_names.add(EVIDENCE_FILE)
    with tempfile.TemporaryDirectory(prefix=".parquet-", dir=destination.parent) as directory:
        normalized = Path(directory)
        write_parquet_dataset(bundle, normalized)
        if evidence is not None:
            publish_import_evidence(normalized, evidence)
        if cancelled():
            raise RuntimeError("CANCELLED: 数据任务已取消")
        for name in old_names:
            (destination / name).unlink()
        for path in normalized.rglob("*"):
            if path.is_file():
                target = destination / path.relative_to(normalized)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
    _emit(emit, "DATA_VALIDATED", "数据集结构及覆盖已验证")
    return {**market_result, "state": "complete", "dataset_version_id": None,
            "coverage": {"data_start": bundle.manifest.data_start.isoformat(), "data_end": bundle.manifest.data_end.isoformat()},
            "price_mode": bundle.manifest.price_mode, "limitations": bundle.manifest.limitations}


def execute_strategy_validation(payload: dict, cancelled) -> dict:
    """Dynamic protocol validation runs only in a worker process."""
    if cancelled():
        raise RuntimeError("CANCELLED: 策略验证已取消")
    store = WorkbenchStore(Path(payload["workspace"]))
    strategy = store.get_strategy(payload["strategy_revision_id"])
    actual = digest(json_bytes({name: digest(content) for name, content in source_files(strategy.path).items()}))
    if actual != strategy.content_hash:
        raise ValueError("SNAPSHOT_INVALID: 策略修订文件已变化")
    entry = strategy.manifest["entrypoint"].partition(":")[0]
    path = strategy.path / entry
    ast.parse(path.read_bytes(), filename=entry)
    params = validate_parameters(strategy.manifest["parameter_schema"], payload.get("params", {}))
    sys.path.insert(1, str(strategy.path))
    spec = importlib.util.spec_from_file_location("quanto_user_validation", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    instance = module.create_strategy(params)
    valid = type(getattr(instance, "warmup_bars", None)) is int and instance.warmup_bars >= 0 and all(
        callable(getattr(instance, name, None)) for name in ("should_evaluate", "on_close"))
    return {"valid": valid, "warmup_bars": instance.warmup_bars if valid else None}
