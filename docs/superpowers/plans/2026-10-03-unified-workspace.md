# Unified workspace and backtest comparison implementation plan

**Goal:** Keep fixed strategies and ETF PPO in one active research workspace and compare their final backtests from `/runs`.

**Design:** Preserve `data/research-20261001` as the canonical workspace. Import completed learning jobs, immutable datasets and artifacts from `data/deeplearning` after backing up both databases. Preserve identifiers and every frozen file byte; update only database location pointers and queue sequence numbers. Keep the source as an offline recovery copy. Serve the canonical workspace on port 8770 and stop the duplicate service on 8767.

**Authorization:** The user explicitly requested one workspace and a combined backtest list and previously authorized autonomous design and implementation without further confirmation. Work continues in the existing `deeplearning` branch. The unrelated untracked PRD is excluded.

**Architecture:** Add a read-only catalog over fixed, policy and prediction backtest jobs; retain their native detail/submit interfaces. Normalize saved metrics and research conditions without modifying historical results. Replace the run-record listing with the catalog and add metric sorting, filters and explicit selected-result comparison. Do not declare a universal winner across incompatible research conditions.

**Tech stack:** Existing Python/SQLite/FastAPI and React/TypeScript; no new dependencies.

## Contract

`GET /api/backtests/catalog?cursor=&limit=50` returns `{items,next_cursor}` in append-stable enqueue order. Each item has `id`, `job_id`, `kind` (`fixed|policy|model`), `name`, `method`, `state`, timestamps, `error`, `start`, `end`, `symbols`, `metrics`, `conditions`, `comparison_key`, `detail_url`, and optional `result_error`. Include unsuccessful and pending backtests; exclude training jobs. Cursors are opaque.

Conditions include period, dataset content hash, symbols, price mode, execution, risk, initial cash, metric definitions and valuation frequency. Identical known conditions across fixed/PPO share a key; incomplete conditions never establish comparability. Decisions at different frequencies remain different strategies, not necessarily different accounting conditions.

## Implementation and verification

- [ ] Backend: add `src/quanto/workbench/backtest_catalog.py` and authenticated route. Test mixed kinds/states, pagination, cross-family comparisons, changed periods/costs, and damaged output isolation in `tests/integration/test_backtest_catalog.py`; run related API tests and commit.
- [ ] Frontend: update RunsPage/API/types, add combined filtering/sorting/comparison and learned-result deep links. Review existing tests against current requirements, preserve meaningful submission/idempotency tests. Run web tests/typecheck/build and commit.
- [ ] Migration: add a narrowly scoped offline learning-workspace import script. Test dry run, active-job rejection, collision rejection, source/destination backups, original run preservation, artifact/input hash preservation and usable relocated references. Never overwrite source files or existing destination objects. Run migration tests and commit.
- [ ] Local integration: check both services idle; stop them, back up databases, import, verify all 47 fixed runs plus 4 policy backtests and 1 policy. Start one service at 8770 using the original research workspace. Validate historical detail/results, imported policy/backtest/export, and a new job in the shared workspace. Validate combined UI and comparisons. Document the verified counts, canonical launch command and recovery backups; commit docs.

## Review focus

- Historical frozen JSON contains original locations: preserve bytes and hashes while ensuring new submissions use relocated registered references.
- Different dates, ETF baskets, data versions, costs or risk settings must not silently produce a comparable label.
- A broken result must remain visible without removing other rows or fabricating metrics.
- Queued/failed backtests and final empty/null metrics must not sort above valid values.
- Existing strategy batch submission, pagination, detail links and new policy submissions must remain functional after consolidation.

## Execution notes

Backend and frontend are delegated independently; root owns migration, service consolidation and live verification. Tests precede implementation for changed behavior. Each independently verified phase is committed locally; no push.
