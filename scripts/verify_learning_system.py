"""Reproducible SYNTHETIC minute-data acceptance through the real API/scheduler.

Run from the checkout: python -m scripts.verify_learning_system --device auto
The default fixture is 115,200 raw 1-minute bars, never real market evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import io
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback
import zipfile

import pandas as pd
import psutil


def directory_bytes(root):
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            pass  # The supervised worker atomically moves staging/checkpoints.
    return total


class ResourceSampler:
    """Sample this process and its worker descendants, including disk caches."""

    def __init__(self, root):
        self.root = root
        self.process = psutil.Process()
        self.stop_event = threading.Event()
        self.peak_rss = self.peak_disk = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def sample(self):
        resident = 0
        try:
            processes = [self.process, *self.process.children(recursive=True)]
        except psutil.NoSuchProcess:
            processes = [self.process]
        for process in processes:
            try:
                resident += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self.peak_rss = max(self.peak_rss, resident)
        self.peak_disk = max(self.peak_disk, directory_bytes(self.root))

    def run(self):
        while not self.stop_event.wait(.5):
            self.sample()

    def stop(self):
        self.stop_event.set()
        self.thread.join()
        self.sample()


def synthetic_snapshot(root, symbols, days, seed):
    from quanto.data.catalog import MarketCatalog
    from quanto.data.contracts import (CoverageEntry, CoverageReport, DataQuery, DataRequest,
        DataRequirement, InstrumentId, NormalizedChunk, PriceBasis, SnapshotManifest, SourceRecord)
    from quanto.data.storage import MarketStore

    # Weekdays, deliberately no claim to reproduce the real exchange holiday calendar.
    sessions = []
    day = datetime(2024, 1, 2, tzinfo=timezone.utc)
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    start = sessions[0] + timedelta(hours=1, minutes=30)
    end = sessions[-1] + timedelta(hours=7)
    instruments = tuple(InstrumentId(exchange="SZSE", instrument_type="etf", code=f"SYNTH{index:04d}")
                        for index in range(symbols))
    query = DataQuery(kind="bars", instruments=instruments, start=start, end=end,
        columns=("open", "high", "low", "close", "volume", "amount"), frequency="1m", price_basis=PriceBasis(mode="raw"))
    calendar_request = DataRequest(kind="calendar", start=start, end=end,
        columns=("trading_date", "session_start", "session_end"))
    limitations = ("synthetic acceptance data; not real market prices or investment evidence",
                   "synthetic weekdays and sessions; exchange holidays/trading status/corporate actions not modeled")
    source = SourceRecord(provider="synthetic_acceptance", interface="deterministic_formula_v1",
        upstream="scripts.verify_learning_system; generated, not downloaded", downloaded_at=end,
        parameters={"seed": seed, "symbols": symbols, "days": days}, actual_mode="raw",
        limitations=limitations, units={"volume": "share", "amount": "CNY"})
    store, catalog = MarketStore(root), MarketCatalog(root)
    fragments, calendar = [], []
    for day_index, day in enumerate(sessions):
        rows = []
        for session_index, hour in enumerate((1, 5)):
            begin = day + timedelta(hours=hour, minutes=30 if hour == 1 else 0)
            finish = begin + timedelta(minutes=120)
            calendar.append(dict(trading_date=day.date(), session_start=begin, session_end=finish))
            for minute in range(120):
                stamp = begin + timedelta(minutes=minute)
                ordinal = day_index * 240 + session_index * 120 + minute
                for symbol_index, instrument in enumerate(instruments):
                    phase = (ordinal + seed + symbol_index * 13) / 37
                    price = 10 + symbol_index * .3 + ordinal * .00002 + .12 * math.sin(phase)
                    close = price + .004 * math.sin(phase + .3)
                    volume = 20000 + (ordinal * 31 + symbol_index * 97 + seed) % 10000
                    rows.append(dict(instrument_id=instrument.key, trading_date=day.date(), bar_start=stamp,
                        bar_end=stamp + timedelta(minutes=1), open=price, high=max(price, close) + .01,
                        low=min(price, close) - .01, close=close, volume=volume, amount=volume * (price + close) / 2))
        frame = pd.DataFrame(rows)
        evidence = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
        fragments.extend(store.put_chunk(NormalizedChunk(query, frame, source, evidence)))
    frame = pd.DataFrame(calendar)
    fragments.extend(store.put_chunk(NormalizedChunk(calendar_request, frame, source,
        frame.to_csv(index=False, lineterminator="\n").encode("utf-8"))))
    requirement = DataRequirement(requests=(query, calendar_request))
    coverage = CoverageReport(entries=tuple(CoverageEntry(request=request) for request in requirement.requests), limitations=limitations)
    ref = store.write_manifest(SnapshotManifest(requirement=requirement, fragments=tuple(fragments), coverage=coverage))
    catalog.create_request("synthetic-acceptance", requirement)
    catalog.commit_ready(("synthetic-acceptance",), ref, ())
    return dict(name="SYNTHETIC acceptance dataset", snapshot_id=ref.id, query=query.model_dump(mode="json")), sessions


def run_pipeline(root, args, report):
    from fastapi.testclient import TestClient
    from quanto.api.app import create_app
    from quanto.learning.registry import verify_ref
    from quanto.learning.service import LearningService

    started = time.perf_counter()
    prepare, sessions = synthetic_snapshot(root / "market", args.symbols, args.days, args.seed)
    report["stages"]["generate_snapshot"] = {"seconds": time.perf_counter() - started, "snapshot_id": prepare["snapshot_id"]}
    prepare["frequency_minutes"] = args.frequency
    train_days, validation_days = int(args.days * .6), int(args.days * .2)
    boundary = lambda index: (sessions[index] + timedelta(hours=1, minutes=30)).isoformat()
    training = dict(name="SYNTHETIC acceptance model", model_type=args.model, train_start=boundary(0),
        train_end=boundary(train_days), validation_start=boundary(train_days), validation_end=boundary(train_days + validation_days),
        test_start=boundary(train_days + validation_days), test_end=prepare["query"]["end"],
        lookback=args.lookback, horizon=2, epochs=args.epochs, batch_size=args.batch_size, learning_rate=.001,
        hidden_size=16, layers=1, seed=args.seed, device=args.device, patience=3)
    app = create_app(root / "workspace")
    learning = LearningService(app.state.service)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/session").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        def request(method, path, **kwargs):
            response = client.request(method, path, headers=headers, **kwargs)
            response.raise_for_status()
            return response

        def submit(stage, path, body, resource):
            begin = time.perf_counter()
            job = request("POST", path, json=body).json()
            report["stages"][stage] = {"job_id": job["id"], "request": body}
            print(f"{stage}: submitted {job['id']}", flush=True)
            deadline = time.monotonic() + args.timeout
            while job["state"] in {"queued", "running", "cancel_requested"}:
                if time.monotonic() >= deadline:
                    request("POST", f"/api/jobs/{job['id']}/cancel", json={})
                    raise TimeoutError(f"{stage} exceeded {args.timeout}s; cancellation requested")
                time.sleep(.2)
                job = request("GET", f"/api/jobs/{job['id']}").json()
            if job["state"] != "succeeded":
                record = app.state.service.store.get_job(job["id"])
                trace = record.artifact_root / record.id / "control" / "traceback.txt"
                raise RuntimeError(f"{stage}: {job}\n{trace.read_text(encoding='utf-8') if trace.exists() else ''}")
            ref = learning.reference(job["id"], {"datasets": "dataset", "models": "model", "backtests": "backtest"}[resource])
            verify_ref(ref)
            detail = request("GET", f"/api/learning/{resource}/{job['id']}").json()
            report["stages"][stage].update(seconds=time.perf_counter() - begin, state=job["state"],
                content_hash=ref["content_hash"], files=ref["files"], manifest=detail["manifest"])
            print(f"{stage}: succeeded in {report['stages'][stage]['seconds']:.2f}s", flush=True)
            return job["id"], detail["manifest"]

        report["capabilities"] = request("GET", "/api/learning/overview").json()["capabilities"]
        dataset_id, dataset = submit("prepare", "/api/learning/datasets", {"spec": prepare}, "datasets")
        expected_rows = args.symbols * args.days * 240 // args.frequency
        if dataset["row_count"] != expected_rows or dataset["statistics"]["source_rows"] != report["raw_rows"]:
            raise AssertionError("prepared/source row count mismatch")
        report["prepared_rows"] = dataset["row_count"]
        report["stages"]["prepare"]["rows_per_second"] = dataset["row_count"] / report["stages"]["prepare"]["seconds"]
        model_id, model = submit("train", "/api/learning/train", {"dataset_id": dataset_id, "spec": training}, "models")
        if model["cutoff"] != training["validation_end"] or any(model["metrics"][split]["samples"] <= 0 for split in ("train", "validation", "test")):
            raise AssertionError("invalid frozen cutoff or empty chronological partition")
        if args.device != "auto" and model["runtime"]["device"] != args.device:
            raise AssertionError("requested device was not used")
        if not any("synthetic" in item for item in model["limitations"]):
            raise AssertionError("synthetic provenance was lost")
        report["runtime"] = model["runtime"]
        backtest_spec = dict(start=training["test_start"], end=training["test_end"], baseline="model",
            initial_cash=100000, top_k=min(3, args.symbols), rebalance_every=15,
            max_total_weight=.9, max_symbol_weight=.4, commission_rate=.0003, minimum_commission=5,
            slippage_rate=.0005, participation_rate=.1, lot_size=100, settlement_days=1)
        result_id, summary = submit("model_backtest", "/api/learning/backtests",
            {"dataset_id": dataset_id, "model_id": model_id, "spec": backtest_spec}, "backtests")
        test_days = args.days - train_days - validation_days
        if summary["counts"]["rows"] != args.symbols * test_days * 240 // args.frequency:
            raise AssertionError("backtest evaluated row count mismatch")
        table = request("GET", f"/api/learning/backtests/{result_id}/tables/equity?limit=2").json()
        chart = request("GET", f"/api/learning/backtests/{result_id}/charts?max_points=25").json()
        if len(table["items"]) != 2 or len(chart["points"]) > 25:
            raise AssertionError("bounded result queries failed")
        export = request("GET", f"/api/learning/backtests/{result_id}/export")
        with zipfile.ZipFile(io.BytesIO(export.content)) as bundle:
            if bundle.testzip() is not None or "summary.json" not in bundle.namelist():
                raise AssertionError("result export is invalid")
        report["checks"] = {"artifact_hashes": True, "chronological_cutoff": True, "nonempty_partitions": True,
            "source_and_prepared_rows": True, "backtest_rows": True, "paged_table": True, "bounded_chart": True, "zip_export": True}
        for baseline in args.baselines:
            submit(baseline, "/api/learning/backtests", {"dataset_id": dataset_id,
                "spec": {**backtest_spec, "baseline": baseline}}, "backtests")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=8)
    parser.add_argument("--days", type=int, default=60, help="synthetic weekdays, 240 minutes each")
    parser.add_argument("--frequency", type=int, choices=(1, 5, 15, 30, 60), default=1)
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--model", choices=("linear", "gru"), default="gru")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--lookback", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--threads", type=int, default=4, help="OMP/MKL worker thread limit")
    parser.add_argument("--timeout", type=float, default=1800, help="per-job timeout seconds")
    parser.add_argument("--baselines", nargs="*", choices=("cash", "equal_weight", "momentum"), default=["cash", "equal_weight", "momentum"])
    parser.add_argument("--output", type=Path, default=Path("outputs/learning-verification.json"))
    parser.add_argument("--temp-parent", type=Path, default=Path("outputs/learning-acceptance"), help="scratch parent; always creates a new child")
    parser.add_argument("--keep-artifacts", "--keep-workspace", dest="keep_artifacts", action="store_true", help="retain the isolated scratch directory for inspection")
    args = parser.parse_args(argv)
    if not (1 <= args.symbols <= 128 and 5 <= args.days <= 1000 and args.threads >= 1 and args.timeout > 0):
        parser.error("symbols 1..128, days 5..1000, threads >=1, timeout >0 required")
    scratch_parent = (args.temp_parent or Path(tempfile.gettempdir())).resolve()
    scratch_parent.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="quanto-learning-acceptance-", dir=scratch_parent)).resolve()
    old_env = {name: os.environ.get(name) for name in ("QUANTO_MARKET_CONFIG", "OMP_NUM_THREADS", "MKL_NUM_THREADS")}
    report = dict(schema_version=1, status="running", synthetic=True,
        warning="Synthetic functional/capacity acceptance only; no real-market profitability evidence.",
        started_at=datetime.now(timezone.utc).isoformat(), python=sys.version, platform=platform.platform(),
        verification_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        logical_cpus=psutil.cpu_count(), physical_cpus=psutil.cpu_count(logical=False), total_memory_bytes=psutil.virtual_memory().total,
        parameters={key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        raw_rows=args.symbols * args.days * 240, scratch_path=str(root), stages={})
    sampler = ResourceSampler(root)
    begin = time.perf_counter()
    try:
        # Capacity guard is deliberately conservative, not a storage forecast.
        report["disk_free_before_bytes"] = shutil.disk_usage(root).free
        if report["disk_free_before_bytes"] < report["raw_rows"] * 2048 + 512 * 1024**2:
            raise ValueError("insufficient scratch space; reduce --days/--symbols or choose --temp-parent")
        config = root / "market-config.json"
        config.write_text(json.dumps({"root": str(root / "market")}), encoding="utf-8")
        os.environ.update(QUANTO_MARKET_CONFIG=str(config), OMP_NUM_THREADS=str(args.threads), MKL_NUM_THREADS=str(args.threads))
        sampler.thread.start()
        run_pipeline(root, args, report)
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=str(exc), traceback=traceback.format_exc())
        print(report["traceback"], file=sys.stderr, flush=True)
    finally:
        if sampler.thread.is_alive():
            sampler.stop()
        report["resources"] = dict(elapsed_seconds=time.perf_counter() - begin, peak_process_tree_rss_bytes=sampler.peak_rss,
            peak_scratch_disk_bytes=sampler.peak_disk, final_scratch_disk_bytes=directory_bytes(root),
            sample_interval_seconds=.5, gpu_peak_memory_bytes=None,
            measurement_notes="RSS sums parent and worker descendants (shared pages may count twice); disk sums logical file sizes. Sampled peaks are lower bounds; GPU allocation peak is not instrumented.")
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        report["artifacts_retained"] = args.keep_artifacts
        if not args.keep_artifacts:
            # Verify the final resolved target before recursive deletion on Windows.
            target = root.resolve()
            if target.parent != scratch_parent or target == scratch_parent or root.is_symlink() or not target.name.startswith("quanto-learning-acceptance-"):
                report.update(status="failed", error="unsafe scratch cleanup target; artifacts preserved", artifacts_retained=True)
            else:
                shutil.rmtree(target)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"{report['status']}: {output}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
