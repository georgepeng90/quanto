# Training Models Implementation Plan

> **For agentic workers:** use the plan task-by-task, with test-driven development and verification before completion. Coordinated implementation uses disjoint file ownership; root integrates and performs whole-branch review.

**Goal:** 固定 ETF 篮子的持久模型、追加训练、本地版本和指定版本回测，以及四域工作台。

**Architecture:** Reuse PPO/Gymnasium, immutable job inputs, learning artifact publication, and unified backtest catalog. Add model/session identity to the workspace DB; keep latest training state separate from validation-best inference snapshot.

**Tech Stack:** Python 3.13 / FastAPI / SQLite / SB3 PPO / React / TypeScript.

**Spec:** `docs/superpowers/specs/2026-10-04-training-models-design.md`

## Global Constraints

- Work in current `D:/Projects/quanto`, branch `training`; no push and no new worktree.
- No actual research training, no basket selection; isolated synthetic test training only.
- Preserve original market data, fixed strategies, all historical backtests, and unrelated untracked PRD.
- Same-model continuation loads last parameters AND optimizer; inference uses immutable validation-best policy.
- A model fixes ordered basket, dataset/research configuration; only additional budget/device change by session.
- Four primary nav items only: 行情数据 / 固定策略 / 强化学习模型 / 回测.
- Stage-specific local commits after applicable checks; no user approval gates per explicit authorization.

## Review Focus

- Concurrent sessions for one model: atomic association and active-job exclusion.
- Continuing after a worse validation run: incumbent selected policy remains unchanged, last state advances.
- Cancelling or failure: old versions remain usable; no phantom successful version.
- Missing/undefined Sharpe: meaningful selection without NaN or fabricated scores.
- Old deep links and pinned backtest versions: readable reports and immutable input identity.

### Task 1: Resumable PPO state and validation selection

**Files:** investing/training.py, contracts.py, evaluation.py; tests/unit/test_investing_training.py plus focused continuation tests.

**Interfaces:** `train_policy(..., dataset_content_hash, *, resume_dir=None, model_id=None, evaluate_test=True)` returns a standard policy manifest. Managed model jobs pass `evaluate_test=False`. `resume_dir` is a verified previous successful policy directory. Add selection_metric to PolicyTrainingSpec. Final artifact contains policy.zip and last.zip. Manifest includes model_id, parent_content_id, timesteps_started, timesteps_completed (cumulative), session_timesteps, best_timesteps, history, selection metric and validation metrics (Sharpe included).

- [ ] Review existing training tests; add failing continuation, incumbent selection, Sharpe-null and no-test-evaluation tests.
- [ ] Implement PPO.load continuation with optimizer and fresh account, cumulative progress, best incumbent copy, last save after final update.
- [ ] Verify unit training suite and existing environment behavior with synthetic fixtures; commit owned files.

### Task 2: Persistent models, session API and Agent CLI

**Files:** new investing/models.py; investing/api.py, jobs.py; learning/service.py, registry.py; cli.py; integration tests.

**Interfaces:** Exact model API and response fields in spec. DB tables `investing_models` and `investing_sessions`; session version equals succeeded job artifact id. Submission transaction hook associates session and rejects simultaneous writers. `policy_resume_ref` is frozen trusted previous artifact; `model_id` in managed requests controls evaluate_test=False. Existing policy backtest dispatch receives model_id provenance.

- [ ] Add failing create/list/detail/continue/concurrency/wrong-version/CLI tests, updating obsolete unsupported models-route test.
- [ ] Add idempotent schema and model service; validate data and ordered symbols before create; share atomic job insertion transaction with session association.
- [ ] Freeze latest successful continuation ref and invoke Task 1; managed backtests require explicit owned version.
- [ ] Verify integration tests with temporary workspaces, including real short train → continue → frozen version backtest; commit owned files.

### Task 3: Four-domain UX, models list/detail and model backtests

**Files:** web/src/App.tsx, api.ts, types.ts, styles.css; new model pages/forms and result route; RunsPage, InvestingPage, LearningPage, BacktestCatalog as necessary; frontend tests/e2e.

**Interfaces:** Task 2 APIs as specified. `/models`, `/models/:id`, `/runs?model=ID&version=ID`; old report deep links remain readable in result-only routes. Backtest history receives refresh after submission.

- [ ] Review existing tests against four-domain requirements; write failing nav/create-without-training/continue/version-choice cases.
- [ ] Build list/detail with creation separate from training, frozen ordered basket, additive budget, cancellation copy and explicit version backtests.
- [ ] Put history first and new-backtest forms under a secondary action; retain fixed batch submissions and historical report detail/export.
- [ ] Run frontend tests, typecheck, build; commit owned files.

### Task 4: Integrated acceptance, review, and documentation

**Files:** docs/07_ETF自主投资训练指南.md, README.md, new acceptance record; focused fixes as needed.

- [ ] Run full Python and web suites, production build, isolated browser/e2e checks covering created model and report navigation.
- [ ] Independent whole-branch review; address material findings with regression coverage.
- [ ] Update guides/Agent API examples, describe session budgets and cancellation/resume boundary, record tests and no actual training.
- [ ] Commit docs/fixes; verify clean stage scope and branch, leave actual model creation/training to user's future basket discussion.

## Progress

- Design: two independent UX/investment-platform reviews complete; no approvals required by user.
- Pre-flight: Task 1 manifest -> Task 2 registry uses existing policy artifact; Task 2 response -> Task 3 matches explicit field contract. No known conflicts.
- Decision: finite additive sessions with successful-version continuation; cancelling discards unpublished current progress. This avoids introducing an infinite queue or claiming exact replay/recovery that is not implemented.
