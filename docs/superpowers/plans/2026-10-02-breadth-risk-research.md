# Breadth Risk Research Implementation Plan

> **For agentic workers:** Use subagent-driven development for the candidate module and independent audits; the primary agent owns execution, selection, publication and synthesis.

**Goal:** Test the user's approved volatility sizing and breadth hysteresis ideas against run `6a2eab55-4495-4c3f-8e73-4220646a635a`, balancing return, drawdown and Sharpe.

**Architecture:** Add research scripts and tests; retain the engine, existing strategies, data and Round 3 artifacts. Use the same 305-ETF audited snapshot, costs and four periods. Freeze all settings and implementation hashes before running; every configuration remains visible.

**Tech Stack:** Existing Python 3.13 environment, pandas/numpy, quanto backtest engine, local workbench API.

**Spec:** User approved the preceding in-chat proposal by saying “继续”: keep the breadth strategy as baseline, test volatility sizing and distinct entry/exit breadth thresholds, and examine nearby parameters. No trading or deployment is requested.

## Global Constraints

- Preserve the unrelated `docs/planning-artifacts/prd.md`; commit only this research's files after each verified stage. Do not push.
- Reuse actual cached prices only after hash and integrity checks; no synthetic prices, backfills or changed data definitions.
- Same historical universe and known survivor/catalog/adjustment limitations as Round 3. These already inspected dates are not an independent holdout.
- Development: 2023-10-09 through 2024-12-31; validation: 2025-01-02 through 2025-12-31; late: 2026-01-05 through 2026-09-30; full: 2023-10-09 through 2026-09-30. Each run starts in cash with inactive hysteresis.
- Use original 200-session trend, 120-session momentum, liquid group representatives, weekly evaluation, next-open execution, five fixed slots, 80% ceiling and all existing engine risk rules.

## Experimental Design (immutable after initialization)

`BreadthRiskStrategy(entry_breadth=.5, exit_breadth=.5, target_vol=None, vol_window=60, gross_target=.8)`:

- At each weekly evaluation, inactive state activates if breadth >= entry threshold; active state deactivates if breadth < exit threshold; otherwise preserve state. No eligible groups resets inactive. Nonweekly calls do not change state. Equal .5 thresholds reproduce the original gate.
- Same representatives and top five positive 120-session momentum signals above SMA200. Each occupied slot starts at gross_target/5; unoccupied slots stay in cash.
- Volatility estimates the current proposed basket using its fixed-slot weights and 60 historical daily returns on common last 61 observed market dates, ending at asof. No forward fill or zero substitution for missing prices; if any selected price is missing/invalid on this grid, target cash and report unavailable risk history.
- Multiply every proposed weight by min(1, target_vol / annualized population standard deviation), with annualization sqrt(252). Zero volatility uses scale 1. No leverage and no redistribution of empty slots.
- Breadth state is a signal state, independent of executed holdings; existing engine risk may delay execution as already documented in Round 3.

Ten new settings:

| Family | Names / settings |
|---|---|
| hysteresis | h55_45, h55_40, h60_45, h60_40 (entry/exit percentages) |
| volatility | vol10, vol12, vol15 (50/50 breadth, 60-day basket risk, annual targets 10/12/15%) |
| combined | h55_45_vol10, h55_45_vol12, h55_45_vol15 |

Two controls: `breadth_original` = unmodified Round3Strategy(breadth_timing, 1, .8); `breadth_static60` = same original strategy with .6 gross target. Static reduction helps distinguish dynamic risk sizing from simply investing less.

All 12 settings run development and validation. Select one winner per new family using the largest min(development, validation) value of annual_return - 0.5*max_drawdown, then the largest minimum Sharpe, then validation Sharpe, then name. Nonfinite/missing Sharpe ranks last. Freeze these three selections before running late/full. All 12 still run late/full for transparent neighborhood analysis; later results do not retroactively alter selection.

Stress the three selected candidates and original baseline at double commission, minimum commission and slippage for full/late. This gives 48 normal-cost plus 8 stressed runs. Original baseline metrics must exactly reproduce Round 3 in every period and both stress periods.

Report the previous gate unchanged (full CAGR >=8%, drawdown <=15%, Sharpe >=.7, all three subperiod returns positive), and deltas from baseline. Report any better late/full result outside the shortlist as exploratory, with no claim of independent validation. Examine each complete parameter family, not only its best point.

## Review Focus

1. Future history cannot affect signals or risk estimates: test explicit future rows.
2. Hysteresis must hold state in the band, treat equality correctly and reset for each new run: sequence tests.
3. Volatility sizing must include covariance and cash slots, and never amplify exposure: hand-calculated basket returns and missing-history tests.
4. Unmodified gate must match original targets; all-period real baseline replay must match prior exact metric dictionaries.
5. Resume must reject changed inputs/artifacts and selection must not use late/full: reuse checked Round 3 evidence helpers and test selection with tempting late-period outcomes.

## Execution Tasks

### 1. Candidates

- Create `scripts/research_round4_candidates.py` and `tests/unit/test_research_round4_candidates.py`.
- Expose `BreadthRiskStrategy`, `configurations()` returning ten new configs with kind `breadth_risk`, and `families()` returning the three name lists. Reuse Round 3 candidate helpers without modifying frozen files.
- Write meaningful tests, observe expected failures, implement the strategy, run candidate tests and commit those two files.

### 2. Frozen runner and selection

- Create `scripts/research_round4.py` and `tests/unit/test_research_round4.py`.
- Reuse Round 3 dataset checks, artifact hashes, identity, independent metric reconciliation and storage helpers. Own factory/execution functions so no monkeypatch or frozen-file changes are needed.
- Phases: initialize, development, validation, select, final, stress, audit. `outputs/strategy-research-20261002-round4` contains immutable protocol and per-run artifacts; `docs/research/2026-10-02-round4` contains versioned evidence.
- Test selection ignores late/full rows and ranks by both early periods; fail then implement. Initialize and check the protocol, commit code and frozen protocol, run early phases, freeze/commit selection, then run final/stress/audit.

### 3. Results and workbench

- Independently reconcile metrics and baseline replay; inspect family neighborhoods, cost changes and exposure. Run full project tests once the implementation is final.
- Create `scripts/register_round4_results.py` using current local API and existing verified dataset. Bundle new strategy and Round 3 helper sources, use idempotent submissions and verify every published metric dictionary. Publish each selected candidate for full/late; the existing original baseline remains linked for comparison.
- Create `scripts/report_round4.py` and a standalone HTML comparison with all settings, evidence links and explicit limitations. Verify render or HTML structure and actual data values.
- Commit validated results/report/publication records. Report best supported tradeoff, unsuccessful variants and any remaining limitation; do not promise improvement before measurement.
