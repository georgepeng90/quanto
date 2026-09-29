# Market data storage baseline — 2026-09-30

This is a local synthetic-data baseline for comparing storage formats. It does not represent a complete backtest or predict end-to-end backtest speedups.

## Method

Run `python scripts/benchmark_storage.py` (optional `--symbols N --days N`). The script uses seed `20260930` to generate OHLCV rows for 100 symbols × 2,500 business days (250,000 rows) inside a temporary directory. It writes CSV, Parquet, and SQLite with a `(symbol, date)` index. It measures a complete DataFrame read and a query for the first five symbols' last 250 days. CSV parses dates and then filters; Parquet and SQLite filter before materializing the DataFrame. Each operation has one warm-up and three timed repetitions; the reported time is their median. Every read is checked against the same expected DataFrame for columns, row order/count, dtypes, and exact values.

All measurements use warm local filesystem caches. The synthetic dataset, single-file layout, and isolated reads omit real data access patterns, cache state, concurrent jobs, preprocessing, strategy execution, and other backtest costs. Treat the timings as a format/query microbenchmark only.

## Environment and results

Windows 11, Python 3.13.12, pandas 3.0.6, NumPy 2.5.3, PyArrow 23.0.1. Run on 2026-09-30 with the default 100 × 2,500 parameters.

| Format | File size | Full read, median | 5 symbols × 250 days, median | Rows returned |
| --- | ---: | ---: | ---: | ---: |
| CSV | 24,909,455 bytes | 0.3574 s | 0.3568 s | 250,000 / 1,250 |
| Parquet | 10,899,925 bytes | 0.0089 s | 0.0079 s | 250,000 / 1,250 |
| SQLite, indexed `(symbol, date)` | 27,176,960 bytes | 0.3797 s | 0.0027 s | 250,000 / 1,250 |

The filtered CSV measurement reads and parses the full file before filtering. These results establish only this machine's baseline for this synthetic workload; use representative data and full backtests before drawing production performance conclusions.

## Existing pipeline before changes

`uv run --no-sync python scripts/benchmark_storage_pipeline.py` generated a separate valid six-table dataset with 100 symbols × 2,500 days. The experiment selects five symbols and the last 250 days. Each phase below is one observation, with the OS cache not cleared; the two freeze calls are independent submissions. Dataset creation, strategy execution, journals and reporting are not included in these phase timings.

| Phase | Before (seconds) |
| --- | ---: |
| CSV load plus full validation | 2.0474 |
| Register dataset | 4.1260 |
| First freeze | 3.0255 |
| Second freeze | 2.3974 |
| Verify snapshot (environment check excluded) | 0.0392 |
| Worker-equivalent data load/validation | 2.0367 |

Input and registered version each used 38,604,550 bytes. Two schema-v1 snapshots used 77,927,770 bytes including their code; there was no shared object. Runtime loaded all 250,000 price rows although the strategy selects five symbols. Baseline related dataset/universe/snapshot/worker tests: 66 passed.

The format microbenchmark uses OHLCV; this pipeline dataset also includes turnover and five related tables. Their sizes and timings must not be directly equated.
