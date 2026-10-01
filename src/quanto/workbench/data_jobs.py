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

from quanto.data.fetcher import AKShareDataProvider, build_processed_dataset
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
        allowed = {"manifest.json", *manifest.auxiliary_tables.values()}
        for table in ("prices", "benchmark", "calendar", "instruments", "status", "events"):
            matches = names & {f"{table}.csv", f"{table}.parquet"}
            if len(matches) != 1:
                raise ValueError("IMPORT_TABLES: 数据表必须且只能提供一种格式")
            allowed.update(matches)
        if names != allowed:
            raise ValueError("IMPORT_FILES: 导入包含未声明或缺失文件")
        for item in members:
            safe_name(item.filename)
            if item.is_dir():
                raise ValueError("IMPORT_PATH: 数据集只能包含根目录常规文件")
            path = destination / item.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(item))


def _publish_research(result, calendar: list[str], sessions: list[str], symbols: list[str], payload: dict, destination: Path) -> None:
    prices = pd.read_csv(result.prices_path, dtype={"symbol": str})
    benchmark = pd.read_csv(result.benchmark_path)
    prices.to_csv(destination / "prices.csv", index=False)
    benchmark.to_csv(destination / "benchmark.csv", index=False)
    pd.DataFrame({"date": calendar}).to_csv(destination / "calendar.csv", index=False)
    first = sessions[0]
    instruments = payload.get("instruments")
    if instruments is None:
        instruments = [{"symbol": symbol, "lot_size": 100, "settlement_sessions": 1,
                        "listing_date": first, "group": "research"} for symbol in symbols]
    pd.DataFrame(instruments).to_csv(destination / "instruments.csv", index=False)
    pd.DataFrame([{"date": day, "symbol": symbol, "status": "trading", "can_buy": True, "can_sell": True}
                  for symbol in symbols for day in sessions]).to_csv(destination / "status.csv", index=False)
    pd.DataFrame(columns=["symbol", "event_type", "record_date", "ex_date", "pay_date", "cash_per_share", "split_ratio"]).to_csv(destination / "events.csv", index=False)
    manifest = {"source": f"AKShare qfq research feed; cache version {payload['cache_version']}", "is_demo": False, "price_mode": "adjusted_research",
                "currency": "CNY", "calendar_start": first, "calendar_end": calendar[-1],
                "data_start": first, "data_end": sessions[-1], "volume_unit": "unknown", "amount_unit": "unknown",
                "event_coverage": "unknown", "benchmark_type": "other",
                "field_semantics": {"amount": "AKShare ETF amount, unit unverified", "turnover": "selected index feed amount, not market-wide turnover"},
                "universe_rule": {"min_history_days": 1, "min_avg_turnover_20d": 0},
                "limitations": ["qfq 前复权研究模式不模拟真实分红和拆分现金流。",
                                "交易标志、每手数量和结算周期为研究模型假设，不是真实可交易状态证据。",
                                "首次观测日期仅是研究覆盖边界，未核实真实上市日。",
                                "成交量和成交额单位未核实；不得使用依赖绝对单位的筛选。"]}
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def execute_data_job(kind: str, payload: dict, emit, cancelled) -> dict:
    """Worker-only data operation. Registration is done by the scheduler after exit."""
    if cancelled():
        raise RuntimeError("CANCELLED: 数据任务已取消")
    _emit(emit, "DATA_JOB_STARTED", "数据任务已开始")
    workspace = Path(payload["workspace"])
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
        symbols = payload.get("symbols", [])
        if not symbols or not all(isinstance(symbol, str) and symbol.isdigit() for symbol in symbols):
            raise ValueError("INVALID_SYMBOLS: 请提供 ETF 代码")
        start, end = payload["start_date"], payload["end_date"]
        if "calendar" in payload and not payload["calendar"]:
            _emit(emit, "CALENDAR_INCOMPLETE", "交易日历覆盖不足")
            return {"state": "incomplete", "dataset_version_id": None, "coverage": {}, "reason": "CALENDAR_INCOMPLETE"}
        provider = AKShareDataProvider()
        try:
            calendar = payload["calendar"] if "calendar" in payload else provider.fetch_calendar(start, end)
        except Exception as exc:
            if cancelled():
                raise RuntimeError("CANCELLED: 数据任务已取消") from exc
            _emit(emit, "CALENDAR_INCOMPLETE", "交易日历覆盖不足")
            return {"state": "incomplete", "dataset_version_id": None, "coverage": {},
                    "reason": "CALENDAR_INCOMPLETE", "error": type(exc).__name__}
        calendar = sorted(set(calendar))
        sessions = [day for day in calendar if start <= day <= end]
        if not sessions or not any(day > end for day in calendar):
            _emit(emit, "CALENDAR_INCOMPLETE", "交易日历缺少后续交易日")
            return {"state": "incomplete", "dataset_version_id": None, "coverage": {}, "reason": "CALENDAR_INCOMPLETE"}
        processed = destination / "processed"
        try:
            result = build_processed_dataset(symbols, start, end, processed, workspace / "raw-cache",
                provider=provider, expected_sessions=sessions, cache_version=payload.get("cache_version"),
                request_delay_seconds=payload.get("request_delay_seconds", 1.0), max_retries=payload.get("max_retries", 3),
                cancelled=cancelled)
            if not result.complete:
                _emit(emit, "COVERAGE_INCOMPLETE", "行情或基准覆盖不足",
                      {"missing_sessions": {name: len(item["missing"]) for name, item in result.coverage.items()},
                       "failed_symbols": list(result.failed_symbols)})
                return {"state": "incomplete", "dataset_version_id": None, "coverage": result.coverage,
                        "failed_symbols": list(result.failed_symbols), "reason": "COVERAGE_INCOMPLETE"}
            _publish_research(result, calendar, sessions, symbols, payload, destination)
        finally:
            if processed.exists():
                shutil.rmtree(processed)
    else:
        raise ValueError("UNSUPPORTED_JOB: 未注册的数据任务")
    if cancelled():
        raise RuntimeError("CANCELLED: 数据任务已取消")
    bundle = load_dataset(destination)
    # Only this job's unpublished staging files are replaced; imports and old versions stay intact.
    old_names = {"manifest.json", *bundle.manifest.auxiliary_tables.values()}
    old_names.update(f"{name}.{suffix}" for name in ("prices", "benchmark", "calendar", "instruments", "status", "events")
                     for suffix in ("csv", "parquet") if (destination / f"{name}.{suffix}").exists())
    with tempfile.TemporaryDirectory(prefix=".parquet-", dir=destination.parent) as directory:
        normalized = Path(directory)
        write_parquet_dataset(bundle, normalized)
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
    return {"state": "complete", "dataset_version_id": None,
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
