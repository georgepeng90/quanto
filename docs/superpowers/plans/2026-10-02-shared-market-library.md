# Shared Market Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** All actual A-share data uses one module-owned shared store; verified historical migration retires data/raw.

**Architecture:** Module-global absolute-root configuration replaces workspace ownership. Existing immutable object/evidence contracts remain. Explicit migration imports both source collections, preserves old provenance and chooses one active series per instrument without mixing adjustment bases.

**Tech Stack:** Python 3.13, pandas, PyArrow/Parquet, SQLite, pytest, Playwright Edge.

**Spec:** docs/superpowers/specs/2026-10-02-shared-market-library-design.md

## Global Constraints

- No new dependency; no real cache writes from tests.
- Do not stage concurrent untracked expanded-research scripts/tests or PRD.
- Preserve original evidence bytes, source uncertainty, frozen audit JSON and existing replay datasets.
- Each independently verified phase gets a local commit; merge to v2 after verification.

## Review Focus

- Running from a different cwd/checkout must not change the shared root.
- A fake feed in a test child must never enter production cache.
- Corrupted legacy input or normalized output must block raw cleanup.
- Different adjustment bases must not be combined by wildcard queries.
- A newly populated store must not falsely claim ready without authoritative calendar coverage.

### Task 1: Shared root and consumers

**Files:** data/settings.py, data/market.py, workbench/store.py; import/prefetch/research scripts; test fixtures, market/workbench integration and E2E config; README.
**Interfaces:** get_market_root() -> Path; configure_market_root(root: Path) -> Path; MarketData(root: Path | None = None). Jobs continue consuming a frozen root.

- [ ] Write tests for cwd independence, absolute config validation, two workspaces sharing a root, old workspace root rejection, and task root freezing.
- [ ] Run them RED; implement module resolver, remove workspace root ownership and production CLI root selectors.
- [ ] Isolate pytest/E2E/benchmark global settings; run related contract/workbench/script tests GREEN and commit.

### Task 2: Verified migration and evidence access

**Files:** data/storage.py, data/market.py, scripts/migrate_raw_market_data.py, tests/unit/test_market_migration.py.
**Interfaces:** read_evidence(hash) sync/async validates hash; migrate(source_checkout, root, report_path) imports audited CSVs and writes lineage only after parity; verify_migration(root, report) verifies preserved evidence/objects and registered activity.

- [ ] Write RED cases for original bytes, all numeric/string/date values, corruption, idempotence, compatible activity selection and incomplete coverage disclosure.
- [ ] Implement metadata from frozen audits and explicit source limitations; retain both immutable versions, choose latest Tencent where available.
- [ ] Run GREEN tests, execute real migration from main raw into shared root and verify all 61 files; commit script/tests and portable migration report.

### Task 3: Retire raw and integrate

**Files:** delete tracked data/raw; update current docs, migration pointers and module entry examples.

- [ ] Reverify real migration report; delete raw only after all comparisons pass.
- [ ] Update documentation to identify old paths as retired provenance, not live input/cache.
- [ ] Full Python/web/type/build/Edge checks; fresh whole-branch independent review; fix material findings RED→GREEN.
- [ ] Commit each verified phase, merge into v2 without concurrent files, reverify merged behavior, archive own worktree preserving validation artifacts.
