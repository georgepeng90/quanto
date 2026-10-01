"""Reproducible synthetic daily/minute cache measurements; never downloads prices."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import platform
import tempfile
import threading
import time

import numpy as np
import pandas as pd
import psutil

from quanto.backtest.market_dataset import build_data_requirement
from quanto.data import MarketData, MarketConfig
from quanto.data.contracts import DataQuery, DataRequirement, NormalizedChunk, SourceRecord
from quanto.data.imports import import_table
from quanto.data.providers.base import ProviderCapabilities


def synthetic_case(count, days, frequency="1d"):
    dates = pd.bdate_range("2020-01-02", periods=days)
    symbols = [f"{index + 1:06d}" for index in range(count)]
    payload = dict(symbols=symbols, start_date=str(dates[0].date()), end_date=str(dates[-1].date()), price_mode="raw_cash")
    requirement = build_data_requirement(payload)
    if frequency == "1m":
        requirement = requirement.model_copy(update={"requests": tuple(request.model_copy(update={"frequency": "1m"})
            if request.kind == "bars" else request for request in requirement.requests)})
    source = SourceRecord(provider="synthetic", upstream="seeded_generator", interface="benchmark", actual_mode="raw",
        downloaded_at=pd.Timestamp("2020-01-01", tz="UTC").to_pydatetime(), units={"volume": "share", "amount": "CNY"},
        verified=("units", "price_basis", "calendar", "instruments", "status", "actions"))
    chunks = []
    random = np.random.default_rng(20261001)
    for request in requirement.requests:
        if request.kind == "bars":
            stamps = []
            for day in dates:
                begin = day.tz_localize("Asia/Shanghai").tz_convert("UTC")
                stamps.extend([begin + pd.Timedelta(hours=9, minutes=30)] if frequency == "1d" else
                    [begin + pd.Timedelta(minutes=minute) for minute in (*range(570, 690), *range(780, 900))])
            starts = pd.DatetimeIndex(stamps)
            frames = []
            for instrument in request.instruments:
                values = random.uniform(5, 50, len(starts))
                ends = starts.normalize() + pd.Timedelta(hours=7) if frequency == "1d" else starts + pd.Timedelta(minutes=1)
                frames.append(pd.DataFrame(dict(instrument_id=instrument.key, trading_date=starts.date, bar_start=starts,
                    bar_end=ends, open=values, high=values * 1.01, low=values * .99, close=values, volume=10000., amount=values * 10000)))
            frame = pd.concat(frames, ignore_index=True)
        elif request.kind == "calendar":
            calendar = pd.bdate_range(pd.Timestamp(request.start).date(), (pd.Timestamp(request.end) - pd.Timedelta(days=1)).date())
            local = calendar.tz_localize("Asia/Shanghai")
            frame = pd.concat([pd.DataFrame(dict(trading_date=calendar.date,
                session_start=(local + pd.Timedelta(minutes=begin)).tz_convert("UTC"),
                session_end=(local + pd.Timedelta(minutes=end)).tz_convert("UTC"))) for begin, end in ((570, 690), (780, 900))], ignore_index=True)
        elif request.kind == "instruments":
            frame = pd.DataFrame([dict(instrument_id=item.key, listing_date=dates[0], lot_size=100, settlement_sessions=1, group="synthetic") for item in request.instruments])
        elif request.kind == "trading_status":
            frame = pd.DataFrame([dict(instrument_id=item.key, trading_date=day, status="trading", can_buy=True, can_sell=True) for item in request.instruments for day in dates])
        else:
            frame = pd.DataFrame(columns=request.columns)
        evidence = json.dumps(dict(seed=20261001, synthetic=True, frequency=frequency, rows=len(frame), request=request.model_dump(mode="json")), sort_keys=True).encode()
        chunks.append(NormalizedChunk(request, frame, source, evidence))
    return payload, requirement, tuple(chunks)


class SyntheticProvider:
    name = "synthetic"

    def __init__(self, chunks):
        self.chunks, self.calls = chunks, []

    def capabilities(self):
        return ProviderCapabilities(("bars",), ("1d", "1m"), {"etf": ("open", "high", "low", "close", "volume", "amount"), "index": ("close", "amount")}, ("raw",))

    def fetch(self, request, context):
        context.remaining()
        self.calls.append(request.model_dump(mode="json"))
        for chunk in self.chunks:
            if chunk.request.kind == "bars" and set(request.instruments).issubset(chunk.request.instruments):
                frame = chunk.frame.loc[chunk.frame.instrument_id.isin([item.key for item in request.instruments])
                    & (chunk.frame.bar_start >= request.start) & (chunk.frame.bar_end <= request.end)].copy()
                return NormalizedChunk(request, frame, chunk.source, json.dumps(self.calls[-1], sort_keys=True).encode())
        raise ValueError("synthetic request not generated")


def timed(operation):
    start = time.perf_counter()
    value = operation()
    return value, time.perf_counter() - start


def measure(count=32, days=20, frequency="1d"):
    payload, requirement, chunks = synthetic_case(count, days, frequency)
    provider = SyntheticProvider(chunks)
    with tempfile.TemporaryDirectory(prefix="quanto-market-bench-") as directory:
        root = Path(directory)
        with MarketData(root / "workspace/market", MarketConfig(max_retries=0, max_workers=2, batch_size=4096), (provider,)) as market:
            start = time.perf_counter()
            for chunk in chunks:
                if chunk.request.kind == "bars":
                    cutoff = chunk.frame.bar_start.max().to_pydatetime()
                    chunk = replace(chunk, request=chunk.request.model_copy(update={"end": cutoff}), frame=chunk.frame.loc[chunk.frame.bar_start < cutoff])
                import_table(market.store, market.catalog, chunk.request, chunk.frame, chunk.source, chunk.evidence)
            import_seconds = time.perf_counter() - start
            ref, gap_seconds = timed(lambda: market.wait_ready(market.prefetch(requirement).id, 60))
            gap_calls = list(provider.calls)
            again, repeat_seconds = timed(lambda: market.wait_ready(market.prefetch(requirement).id, 60))
            assert ref == again and provider.calls == gap_calls
            request = requirement.requests[0]
            query = DataQuery(**request.model_dump())
            full, read_seconds = timed(lambda: market.read_bars(ref, query))
            selected, selected_seconds = timed(lambda: market.read_bars(ref, query.model_copy(update={"instruments": request.instruments[:1]})))
            assert len(full) == count * days * (240 if frequency == "1m" else 1)
            assert len(selected) == days * (240 if frequency == "1m" else 1)
            _, verify_seconds = timed(lambda: market.resolve_snapshot(ref.id))
            process = psutil.Process()
            baseline = process.memory_info().rss
            peak, stopped = [baseline], threading.Event()
            def sample():
                while not stopped.wait(.005):
                    peak[0] = max(peak[0], process.memory_info().rss)
            thread = threading.Thread(target=sample)
            thread.start()
            start = time.perf_counter()
            try:
                rows = sum(batch.num_rows for batch in market.iter_batches(ref, query))
            finally:
                stopped.set()
                thread.join()
            batch_seconds = time.perf_counter() - start
            assert rows == len(full)
            fragments = market.catalog.fragments(requirement)
            etf_fragments = [item for item in fragments if item.request.kind == "bars" and item.request.instruments[0].instrument_type == "etf"]
            object_bytes = sum(market.store.object_path(item).stat().st_size for item in etf_fragments)
            cache_bytes = sum(path.stat().st_size for path in market.store.root.rglob("*") if path.is_file())
        workbench_seconds = None
        if frequency == "1d":
            from quanto.workbench.service import WorkbenchService
            service = WorkbenchService(root / "workspace")
            service.store.market_root = root / "workspace/market"
            start = time.perf_counter()
            identity = service.submit_job("fetch", payload)["id"]
            service.scheduler.start(background=False)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    service.scheduler.tick()
                    job = service.store.get_job(identity)
                    if job.state in {"succeeded", "failed", "interrupted", "cancelled"}:
                        break
                    time.sleep(.02)
                assert job.state == "succeeded", service.get_resource("jobs", identity)
                assert service.store.dataset_market_source(identity)["id"] == ref.id
                service.store.get_dataset(identity)
            finally:
                service.scheduler.stop()
            workbench_seconds = time.perf_counter() - start
        return dict(frequency=frequency, symbols=count, sessions=days, etf_rows=len(full), object_bytes=object_bytes,
            object_bytes_per_row=object_bytes / len(full), total_cache_bytes=cache_bytes, gap_request_count=len(gap_calls),
            gap_requests=gap_calls, repeat_request_count=len(provider.calls) - len(gap_calls),
            seconds=dict(import_tables=import_seconds, prepare_gaps=gap_seconds, repeat=repeat_seconds, full_read=read_seconds,
                one_symbol_read=selected_seconds, integrity_verification=verify_seconds, batch_read=batch_seconds, workbench_job=workbench_seconds),
            batch_rss_baseline=baseline, batch_rss_peak=peak[0], batch_rss_increase=peak[0] - baseline,
            methodology="Synthetic seed 20261001, random OHLC, 240 minute bars/session, benchmark plus factual reference tables; one sample, warm OS cache, batch peak sampled every 5ms; workbench time includes frozen child and parent registration; no real source throughput measured.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=32)
    parser.add_argument("--daily-days", type=int, default=2520)
    parser.add_argument("--minute-days", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.symbols < 1 or min(args.daily_days, args.minute_days) < 2:
        parser.error("symbols >= 1 and days >= 2 required")
    result = dict(platform=platform.platform(), python=platform.python_version(), cpu=platform.processor(),
        logical_cpus=psutil.cpu_count(), memory_bytes=psutil.virtual_memory().total,
        cases=[measure(args.symbols, days, frequency) for frequency, days in (("1d", args.daily_days), ("1m", args.minute_days))])
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
