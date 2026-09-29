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

## Pipeline after implementation

Repeated the identical default pipeline script on commit `ad12dd5`, after correctness tests had finished. Same machine, interpreter, synthetic seed and workload; one observation per phase, filesystem cache not cleared. The script checks the selected prices against the original full load with exact DataFrame equality.

| Phase | Before (seconds) | After (seconds) |
| --- | ---: | ---: |
| CSV load plus full validation | 2.0474 | 2.0401 |
| Register dataset | 4.1260 | 2.2294 |
| First freeze | 3.0255 | 2.0493 |
| Second freeze | 2.3974 | 1.9817 |
| Verify snapshot (environment check excluded) | 0.0392 | 0.0190 |
| Worker-equivalent data load/validation | 2.0367 | 0.0416 |

| Stored data | Before (bytes) | After (bytes) |
| --- | ---: | ---: |
| Original CSV input | 38,604,550 | 38,604,550 |
| Registered version | 38,604,550 | 14,030,455 |
| Two run snapshot directories, including code | 77,927,770 | 744,224 |
| Shared dataset objects | 0 | 14,030,455 |
| Two run snapshots **plus shared objects** | **77,927,770** | **14,774,679** |

Runtime materializes 12,500 price rows instead of 250,000: five symbols with all 2,500 days of permitted history, not just the 250-day evaluation window. The large runtime-load reduction combines Parquet, symbol selection, avoiding repeated full coverage validation, and computing eligibility for fewer symbols. It must not be attributed solely to the file format or presented as overall backtest acceleration.

Independent submission freezes still load and validate the complete dataset, so their gains are smaller; CSV input parsing is unchanged. Same-batch validation reuse is separately tested and is not measured by these two independent freeze calls. Integrity verification still reads every shared object's byte. Registered data and its shared immutable copy both consume space, while additional runs reuse the shared copy. There is no claim of zero-copy storage or elimination of all full scans.

## Correctness verification

- Full Python suite: `python -m pytest -q`, **241 passed** in 105.29 seconds on the final implementation.
- Frontend: production build (including TypeScript checking) passed; Chrome Playwright edit/run/compare/export scenario passed in a separate temporary workspace.
- Independent review found legacy Parquet normalization gaps (text dates, numeric symbols and text instrument numbers); regression tests reproduced them and the fixes passed.
- New shared snapshots, concurrent same-object publication, batch atomicity, source changes, object tampering and actual worker execution are covered.
- In addition to a permanent embedded-CSV v1 regression, the original `e3cc24d` code generated a real v1 snapshot in a temporary workspace. The new service executed it, replayed it with identical final output manifest bytes, and cloned it into a successfully executed v2 run. All environment checks remained active; the interpreter and dependencies were the same. Existing preview data was not modified.
