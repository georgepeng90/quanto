from __future__ import annotations

from bisect import bisect_right
from datetime import timedelta
import math
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.parquet as pq

from quanto.data.contracts import BAR_COLUMNS, TableQuery
from quanto.data.market import MarketData
from quanto.learning.artifacts import read_manifest, write_manifest
from quanto.learning.contracts import PrepareSpec, check_cancelled, timestamp
from quanto.learning.features import FEATURE_NAMES, FEATURE_VERSION, causal_features
from quanto.objects import safe_name


def read_dataset_manifest(path: Path) -> dict:
    manifest = read_manifest(path, "dataset")
    if manifest["feature_version"] != FEATURE_VERSION or tuple(manifest["features"]) != FEATURE_NAMES:
        raise ValueError("incompatible dataset feature schema")
    if not manifest["shards"] or any(safe_name(name) not in manifest["files"] for name in manifest["shards"]):
        raise ValueError("dataset shard integrity mismatch")
    return manifest


def prepare_dataset(spec: dict, market_root: Path, output_dir: Path, emit, cancelled) -> dict:
    config = PrepareSpec.model_validate(spec)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("dataset output directory must be empty")
    if shutil.disk_usage(output_dir).free < 64 * 1024 * 1024:
        raise ValueError("insufficient free disk space for dataset preparation")
    statistics = dict(source_rows=0, invalid_rows=0, invalid_feature_bars=0, incomplete_buckets=0, outside_session_rows=0)
    previous, buckets, buffer, shards = {}, {}, [], []
    count, first, last = 0, None, None

    def flush():
        if not buffer:
            return
        if shutil.disk_usage(output_dir).free < 64 * 1024 * 1024:
            raise ValueError("insufficient free disk space while preparing dataset")
        name = f"bars-{len(shards):05d}.parquet"
        pq.write_table(pa.Table.from_pylist(buffer), output_dir / name, compression="zstd")
        shards.append(name)
        buffer.clear()

    with MarketData(Path(market_root)) as market:
        snapshot = market.resolve_snapshot(config.snapshot_id)
        source = market.store.verify_snapshot(snapshot)
        query = config.query
        calendar = market.read_table(snapshot, "calendar", TableQuery(start=query.start, end=query.end, columns=("trading_date", "session_start", "session_end")))
        sessions = sorted((timestamp(row.session_start.to_pydatetime()), timestamp(row.session_end.to_pydatetime()), str(row.trading_date)) for row in calendar.itertuples())
        if not sessions:
            raise ValueError("snapshot contains no trading sessions")
        starts = [item[0] for item in sessions]
        interval = timedelta(minutes=config.frequency_minutes)
        for batch in market.iter_batches(snapshot, query):
            check_cancelled(cancelled)
            for row in batch.to_pylist():
                statistics["source_rows"] += 1
                begin, finish = timestamp(row["bar_start"]), timestamp(row["bar_end"])
                session_index = bisect_right(starts, begin) - 1
                if session_index < 0 or finish > sessions[session_index][1]:
                    statistics["outside_session_rows"] += 1
                    continue
                session_start, session_end, trading_date = sessions[session_index]
                offset = (begin - session_start).total_seconds() / 60
                valid = offset.is_integer() and finish - begin == timedelta(minutes=1)
                valid = valid and all(row[key] is not None and math.isfinite(float(row[key])) for key in ("open", "high", "low", "close", "volume", "amount"))
                valid = valid and min(row["open"], row["high"], row["low"], row["close"]) > 0 and min(row["volume"], row["amount"]) >= 0
                valid = valid and row["high"] >= max(row["open"], row["close"], row["low"]) and row["low"] <= min(row["open"], row["close"])
                if not valid:
                    statistics["invalid_rows"] += 1
                    continue
                bucket_start = session_start + int(offset // config.frequency_minutes) * interval
                key = row["instrument_id"]
                current = buckets.get(key)
                if current and current["bar_start"] != bucket_start:
                    statistics["incomplete_buckets"] += 1
                    current = None
                if current is None:
                    current = {name: row[name] for name in BAR_COLUMNS}
                    current.update(bar_start=bucket_start, bar_end=bucket_start + interval, trading_date=trading_date, _count=0, _next=bucket_start)
                    current["volume"], current["amount"] = 0., 0.
                    buckets[key] = current
                if begin != current["_next"]:
                    continue
                current.update(close=row["close"], high=max(current["high"], row["high"]), low=min(current["low"], row["low"]))
                current["volume"] += row["volume"]
                current["amount"] += row["amount"]
                current["_count"] += 1
                current["_next"] = finish
                if current["_count"] == config.frequency_minutes and current["bar_end"] <= session_end:
                    result = {name: current[name] for name in BAR_COLUMNS}
                    result.update(causal_features(result, previous.get(key)))
                    del buckets[key]
                    if any(not math.isfinite(result[name]) or abs(result[name]) > 3e38 for name in FEATURE_NAMES):
                        statistics["invalid_feature_bars"] += 1
                        continue
                    previous[key] = result
                    result["bar_start"], result["bar_end"] = result["bar_start"].isoformat(), result["bar_end"].isoformat()
                    buffer.append(result)
                    first = first or result["bar_end"]
                    last = result["bar_end"]
                    count += 1
                    if len(buffer) >= 8192:
                        flush()
                        emit(dict(kind="progress", message=f"prepared {count} bars", rows=count))
        statistics["incomplete_buckets"] += len(buckets)
        flush()
        limitations = sorted(set(snapshot.coverage.limitations).union(*(fragment.source.limitations for fragment in source.fragments)))
    check_cancelled(cancelled)
    if not count:
        raise ValueError("no complete valid bars in requested interval")
    result = write_manifest(output_dir, dict(kind="dataset", version=1, name=config.name, query=query.model_dump(mode="json"), snapshot_id=snapshot.id,
        snapshot_digest=snapshot.manifest_digest, frequency_minutes=config.frequency_minutes, instruments=[item.key for item in query.instruments],
        row_count=count, first_bar=first, last_bar=last, feature_version=FEATURE_VERSION, features=list(FEATURE_NAMES), shards=shards,
        trading_days=sorted({day for _, _, day in sessions}),
        timezone="UTC", interval="[start,end)", limitations=limitations, statistics=statistics, price_basis=query.price_basis.model_dump(mode="json")))
    emit(dict(kind="progress", message="dataset prepared", progress=1., rows=count))
    return result


def iter_dataset_bars(dataset_dir: Path, start=None, end=None):
    manifest = read_dataset_manifest(Path(dataset_dir))
    start, end = timestamp(start) if start is not None else None, timestamp(end) if end is not None else None
    for name in manifest["shards"]:
        with pq.ParquetFile(Path(dataset_dir) / name) as parquet:
            for batch in parquet.iter_batches(batch_size=8192):
                for row in batch.to_pylist():
                    if (start is None or timestamp(row["bar_start"]) >= start) and (end is None or timestamp(row["bar_end"]) <= end):
                        yield row
