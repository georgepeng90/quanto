# Market Data Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce repeated backtest data loading/copying without changing strategy results or invalidating historical runs.

**Architecture:** SQLite retains metadata; new datasets use Parquet; run schema v2 references one verified shared dataset object. The worker filters selected symbols and dates up to the run end while preserving all pre-start history.

**Tech Stack:** Existing Python 3.13, pandas, PyArrow, SQLite, pytest. No new dependencies.

**Spec:** [Storage design](../specs/2026-09-30-market-data-storage-design.md).

## Global Constraints

- Preserve real v1 snapshots/reports and their exact replay environment checks; do not rewrite old workspace files.
- Keep original strategy history, eligibility, event, calendar and auxiliary-table semantics.
- No service restart, existing data migration, automatic deletion, push, or UI redesign.
- Commit independently verified stages; do not include the unrelated main-checkout PRD.

## Review Focus

- Long-history strategies with small warmup_bars must receive the same pre-start history (Task 2).
- Empty event tables, string symbols, UTC auxiliary dates must survive Parquet conversion (Task 2).
- Missing/changed shared objects and malicious references must fail before strategy execution (Task 3).
- Repeated or concurrent submissions must not duplicate/corrupt data objects or partially queue a batch (Task 3).
- Historical v1 execution/clone/replay and comparison must retain original meaning (Task 3 and 4).

## Task 1: Baseline and design

Files: this plan/spec; scripts/benchmark_storage.py; scripts/benchmark_storage_pipeline.py; docs/superpowers/benchmarks/2026-09-30-market-data-storage-baseline.md.

- [x] Review existing tests against current requirements; run relevant dataset/snapshot/worker baseline (66 passed).
- [x] Add repeatable temporary-data read and pipeline benchmarks; assert comparable values before timing.
- [x] Record measured methods, times, storage sizes and limits; inspect diffs and commit.

## Task 2: Parquet publication and selective loading

Files: src/quanto/data/storage.py, src/quanto/data/loaders.py, tests/unit/test_storage_loading.py; integration in store.py and data_jobs.py owned by primary agent.

Interfaces: write_parquet_dataset(bundle, destination) -> None; load_dataset_for_run(path, spec) -> DatasetBundle. Runtime path assumes full version validation and hash check have already succeeded.

- [x] Write tests asserting normalized Parquet roundtrip and equivalence to full-load backtest, including eligibility/history/calendar/event boundaries. Observed missing-interface and four publication failures before implementation.
- [x] Implement Parquet writer and filtered loader with conservative preservation of history and event/auxiliary tables.
- [x] Route new registration and successful data jobs through canonical Parquet publication; update tests that assume new versions must contain CSV.
- [x] Run unit dataset/universe/strategy and data-job/revision integration checks; commit Task 2 files. Primary verification: 53 dataset/loading/publication/revision tests and 12 data-job tests passed. Independent scoped review found no important issue.

## Task 3: Shared immutable dataset objects

Files: src/quanto/workbench/dataset_objects.py, snapshots.py, worker.py, runs.py; tests/integration/test_shared_dataset_snapshots.py plus existing snapshot/worker tests as required.

Interfaces: schema v2 adds dataset_ref; fixed object root derived from workspace; v1 continues embedded dataset. freeze retains current call signature; batch-scoped preparation may be added without persistent caches.

- [ ] Write sharing, batch and tamper tests, observe failure before implementation.
- [ ] Implement atomic stream-copy/hash verified shared objects and v2 references; no symlinks or arbitrary path references.
- [ ] Worker resolves and verifies v2 data then calls Task 2 selective loader; v1 uses original full path. Submission reuses preparation within one batch.
- [ ] Exercise actual worker, exact rerun, historical clone/v1, publication failure and concurrent same-object creation; commit Task 3 files.

## Task 4: Integration, measurements and handoff

Files: benchmark results and README/docs/00/docs/05 descriptions of actual storage.

- [ ] Repeat same baseline workloads on final implementation; report load/freeze/verification separately and non-improvements candidly.
- [ ] Run full Python tests; independently review cross-component correctness and fix supported findings.
- [ ] Build the existing UI and run Chrome edit/run/compare/export E2E against temporary workspace.
- [ ] Update operation/backup guidance and completed checklist; commit; fast-forward merge into v2 and verify original PRD unchanged.

## Execution notes

User approved the proposed direction and measurement in conversation. Preserve that authorization; do not add repeat permission gates. Delegated implementation is bounded by explicit file ownership; primary agent owns publication integration, final review coordination and merges.

Ruling: retain full pre-start prices for selected symbols because current StrategyContext allows arbitrary historical reads; date optimization removes post-end prices, not permitted history.

Ruling: Parquet may widen timestamp resolution (empty CSV datetime64[s] becomes datetime64[ms]); compare event date values, not this storage-specific dtype. Midnight event dates retain exact values and the full/filtered transaction-ledger comparison passes.
