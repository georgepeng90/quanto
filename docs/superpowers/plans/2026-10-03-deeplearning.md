# Deep Learning Quantitative Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a local Windows minute-bar preparation, training, model registry and backtesting workflow with a usable workbench and CLI.

**Architecture:** Reuse immutable MarketData snapshots and supervised workbench jobs. Add a bounded-memory learning pipeline and a timestamp-native intraday engine, then connect through a dedicated learning API/page.

**Tech Stack:** Python 3.13, PyTorch CPU/CUDA, NumPy, Arrow/Parquet, SQLite, FastAPI, React/TypeScript.

**Spec:** `docs/superpowers/specs/2026-10-03-deeplearning-design.md`

## Global Constraints

- Work on requested `deeplearning` branch in `D:/Projects/quanto`; no push.
- First release: 1, 5, 15, 30, 60-minute bars; no tick/order-book claims.
- User authorized autonomous design, planning and implementation without approval gates.
- UTC aware `[start,end)` intervals; settlement/interest/performance use trading days.
- Train-only normalization; purge labels crossing splits; validation-only checkpoint selection.
- Bounded batches/windows; no full corpus of overlapping 3-D tensors.
- Preserve the untracked `docs/planning-artifacts/prd.md` and existing research artifacts.
- Each independently verified stage is committed locally.

## Review Focus

- Same-day repeated orders: unique full timestamp IDs and next-bar execution.
- CUDA unavailable or out of memory: actionable failure; requested CUDA never silently becomes CPU.
- Cancellation racing with publication: no registered model or result from a cancelled job.
- Altered dataset/model or incompatible frequency/features: reject before training/inference.
- Split boundary/lunch gap/T+1: no future labels, synthetic candles or next-minute settlement.

## Task 1: Data preparation and trainable model artifacts

**Files:** Create `src/quanto/learning/{__init__,contracts,dataset,features,models,training,inference,artifacts}.py`; tests `tests/unit/test_learning_*.py` and fixtures; root owns `pyproject.toml`/`uv.lock`.

**Interfaces:** Implement exact public signatures in the spec. Dataset manifest exposes name, query, snapshot identity, frequency_minutes, instruments, row_count, first/last bar, feature_version, limitations and file hashes. Model manifest exposes config, dataset identity, feature schema, cutoff, metrics, files. Training spec keys include name, model_type, train_start/end, validation_start/end, test_start/end, lookback, horizon, epochs, batch_size, learning_rate, hidden_size, layers, seed, device, patience.

- [ ] Write tests for causal features, complete session-anchored resampling, train-only scaler, purged labels, immutable hashes and reload equality; observe failure.
- [ ] Implement batch preparation, compact feature storage, lazy windows, chronological partitions, linear and GRU regressors, validation early stopping, checkpoints and batched inference.
- [ ] Run focused unit tests on CPU with small deterministic minute fixtures; verify resume and tamper rejection.
- [ ] Review and commit verified learning core files only.

## Task 2: Timestamp-native intraday backtest

**Files:** Create `src/quanto/learning/intraday.py`, optional focused `intraday_*.py`, and `tests/unit/test_intraday_learning.py`.

**Interfaces:** `run_intraday_backtest(rows, spec, output_dir, emit, cancelled, metadata) -> dict`. `rows` uses spec's row contract, optional `score`. Spec includes start/end, initial_cash, top_k, rebalance_every, max_total_weight, max_symbol_weight, commission_rate, minimum_commission, slippage_rate, participation_rate, settlement_days, lot_size, drawdown_pause, cash_rate, baseline (`model`, `equal_weight`, `momentum`, `cash`). Metadata includes price/source limits. Persist `summary.json`, `equity.csv`, `daily_equity.csv`, `orders.csv`, `fills.csv`, `signals.csv`, `positions.csv`; summary keys metrics, assumptions, counts, period.

- [ ] Write failing behavioral tests for next-open, settlement day, lunch/overnight, cash/lot/participation limits and daily metrics.
- [ ] Implement streaming rows grouped by completed bar, finite account state, daily rollovers, unique orders, bounded output writing, risk and baselines.
- [ ] Verify hand-calculated accounts, future-bar mutation invariance of prior signals, and independent minute metrics.
- [ ] Review and commit only this engine and tests.

## Task 3: Supervised learning jobs and version registry

**Files:** Create `src/quanto/learning/{service,jobs,api}.py`, modify `workbench/{store,models,worker,scheduler}.py`, `api/app.py`, `cli.py`; tests `tests/integration/test_learning_workbench.py`.

**Interfaces:** Spec API paths; jobs `prepare_learning`, `train`, `model_backtest`. Core payloads passed through validated contracts. Training submission takes dataset_id and spec; backtest takes dataset_id, optional model_id and spec. Prepare submission takes spec. Frozen server-side input stores trusted artifact paths and expected hashes; clients cannot inject paths or handlers.

- [ ] Test migration from current jobs CHECK with original rows retained; test authenticated API submission, invalid references and cancellation/publication race.
- [ ] Implement source/input freeze, registry table and conditional transactional registration; worker routes calls to Tasks 1/2, validates artifacts, hashes and receipts.
- [ ] Add overview/pagination/details/results/export and CPU/CUDA capability reporting; add equivalent CLI commands.
- [ ] Test actual supervised CPU pipeline in temporary workspace, cancellation and output querying; commit.

## Task 4: Chinese learning workbench

**Files:** Create `web/src/pages/LearningPage.tsx`, learning components/API/types as needed; modify `web/src/{App,api,types,styles}.tsx/ts/css` minimally; tests under existing frontend test directory.

**Interfaces:** `/api/learning` paths in spec; existing jobs APIs for logs/cancel. Schema dictionaries displayed as structured forms for frequent settings plus JSON visibility. Input dates use datetime-local converted to UTC and labeled Shanghai time.

- [ ] Write failing UI tests for data/model selection, validation, submission, cancellation and result presentation.
- [ ] Implement dataset preparation, training/model lists, progress/logs, backtest parameters, metrics and bounded chart/table results; clear empty state with import guidance.
- [ ] Run frontend focused tests, typecheck and build; review and commit.

## Task 5: Local GPU validation, documentation and end-to-end acceptance

**Files:** Add `scripts/verify_learning_system.py`, `docs/06_深度学习工作台使用指南.md`, small JSON example configs and verification record; update README.

- [ ] Lock optional PyTorch dependency and verify real CPU and RTX CUDA forward/backward, with actual runtime reported.
- [ ] Create deterministic labeled minute fixture in isolated temporary market root; perform prepare/train/saved-model backtest through real scheduler/API and validate hashes/results.
- [ ] Measure larger-than-unit-fixture preparation/training memory, time and storage without occupying the user's full disk; record actual scale only.
- [ ] Run full Python suite and web tests/typecheck/build plus browser workflow; fix meaningful regressions and re-run appropriate checks.
- [ ] Independent whole-branch review focuses on leakage, financial clock/accounting, job atomicity and large-data behavior; resolve important findings.
- [ ] Write user guide with source import, training, checkpoint resume, results, daily-vs-minute distinction and realistic limitations; commit accepted deliverables.

## Execution record

Preflight: Tasks 1 and 2 share the explicit row contract only; Task 3 owns orchestration and adapts result metadata without changing core algorithms. Task 4 consumes the fixed API and owns frontend files. Root serializes commits and dependency changes. User delegated execution-method choice; use parallel implementation in distinct files with review before integration, and final independent review.
