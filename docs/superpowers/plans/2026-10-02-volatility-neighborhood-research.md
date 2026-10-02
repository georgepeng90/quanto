# Round 5: volatility neighborhood research

Goal: refine the user's selected run `8e32c5e8-43f1-4d0b-9f9d-0802f7e0672e` (Round 4 `h55_45_vol12`) for a balance of return and drawdown. This is retrospective research on already inspected prices, with no independent holdout or trading deployment.

## Fixed inputs and implementation

Reuse `BreadthRiskStrategy` and `research_round4.execute` unchanged. Preserve the engine, the seven-file audited 305-ETF snapshot, all historical research artifacts, and the unrelated `docs/planning-artifacts/prd.md`. Freeze this plan, runner, inherited implementation hashes and data hashes before execution. Only the root agent stages and commits; do not push.

Baseline name: `baseline`. Parameters: entry_breadth=.55, exit_breadth=.45, target_vol=.12, vol_window=60, gross_target=.8. All candidates have kind `breadth_risk`. SMA200, positive 120-session momentum, liquid group representatives, weekly signals, next-open execution and five fixed slots remain unchanged.

## Fifteen configurations

- Baseline plus the other eight members of vol_window={30,60,90} x target_vol={.11,.12,.13}; nonbaseline names `w{window}_v{integer percent}`.
- `w20_v12` and `w120_v12` vary only the volatility window.
- `entry50`, `entry65`, `exit35`, `exit50` vary only the named threshold; other baseline parameters remain fixed.

## Periods, selection and execution

Primary periods: development 2023-10-09 to 2024-12-31; validation 2025-01-02 to 2025-12-31; late 2026-01-05 to 2026-09-30; full 2023-10-09 to 2026-09-30. These runs start independently in cash with inactive hysteresis, using available pre-start history for warmup.

1. Run all 15 in development and validation. Verify artifact hashes and independently reconcile metrics.
2. For each candidate calculate the minimum, over the two early periods, of its `(annual_return - .5 * max_drawdown)` minus the baseline's score in that same period. Rank descending by this minimum improvement, then minimum early Sharpe, then validation Sharpe, then ascending name. Missing/nonfinite Sharpe ranks last. Do not subtract the two period minima.
3. Deduplicate using the ordered pair of development and validation `curves.csv` SHA256 hashes. Exclude the baseline's pair, retain every alias and select up to three distinct remaining paths. Select fewer if fewer exist. Record negative scores honestly, without describing selection as demonstrated improvement.
4. Freeze selection and its evidence before any late/full execution. Run all 15 in late/full, including unsuccessful variants.
5. For baseline and the up-to-three selections, run full/late with commission, minimum commission and slippage multiplied by two. Also run normal-cost cash restart sensitivities from 2024-07-01, 2025-07-01 and 2026-04-01 through 2026-09-30. Each restart resets both portfolio and hysteresis state. These are sensitivity checks, not stitched returns or untouched holdouts.

Maximum 80 research runs: 60 primary, 8 cost, 12 restart. Require exact full metric-dictionary equality between the baseline and Round 4 `h55_45_vol12` for all four primary periods and full/late at double cost.

## Verification and delivery

Add a small runner that reuses the existing execution and evidence helpers. Test ranking by period-by-period improvement, ignoring later evidence, hash-pair deduplication/baseline exclusion, and fewer-than-three behavior before implementation. Check all configurations against the frozen strategy. Commit the verified plan, then implementation/protocol, then early selection, then final evidence and publication/report as separately reviewable stages.

Publish full/late of the selected candidates through the local workbench, preserving idempotency receipts and verifying dataset/source identity and exact metric equality. Keep the chosen baseline linked. Additional later winners may be discussed as exploratory; do not change early selection retrospectively. Report all fifteen settings, path aliases, costs, start-date sensitivity, exposure and limitations in a standalone HTML report. Use the unchanged prior descriptive gate (full CAGR >=8%, drawdown <=15%, Sharpe >=.7, all three subperiod returns positive) alongside continuous deltas.

Historical catalog/survivorship and liquidity selection, incomplete adjustment/corporate-action certification, zero cash interest, equity-only universe and execution assumptions remain limitations. The prior June five-symbol price check is a narrow spot check, not full certification. Many same-looking threshold settings were identical in Round 4; neither duplicates nor parameter-search winners constitute independent evidence.
