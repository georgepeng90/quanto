# PPO stagnation experiment implementation plan

> **For agentic workers:** Execute the approved experiment in this session, with independent review of the research scripts. Steps use checkboxes for tracking.

**Goal:** Diagnose the 32ETF control model's cash-heavy validation behavior and compare two learning rates from its round 36 checkpoint.

**Architecture:** Standalone research scripts reuse the existing environment, evaluator and SB3 PPO. They read verified source artifacts and write separate experiment outputs; no model registry or production training identity changes.

**Tech Stack:** Existing Python environment, PyTorch, Stable-Baselines3 2.9.0, pytest.

**Spec:** The two-stage proposal approved in this chat on 2026-10-08. This document records its fixed protocol and implementation steps.

## Fixed protocol

- Source model: `c682a296-7f3b-44c7-b919-492b729e7f9a`.
- Source rounds: 36 (`94d667db-3ae3-45ca-ab75-cf0c4b3022ed`), 300 (`06a0941e-86af-4b99-9c10-c8d6eeb6803e`), 600 (`87e26bfb-b8be-4e35-ac9b-021a02839d22`). Evaluate each `last.zip`, not the retained historical `policy.zip`.
- Dataset: `acd41bfc-b9a1-433f-9109-960beb8ffd6c`, content hash `c4e45bad96649491e40a138ab054c5c94de6e7be38bf156681873cbc8ebd22bd`.
- Training dates: 2022-07-08 through 2024-09-30. Validation dates: 2024-10-01 through 2026-09-30. Preserve basket, costs, risk, reward, network and weekly frequency.
- Diagnosis: one deterministic evaluation plus 20 stochastic evaluations per snapshot, with common evaluation seeds 1000 through 1019. Seed after loading. Record all results, action probabilities, first entry, cash fraction, fills and no-trade cases. Report stochastic median and 10th/90th percentiles; do not select a lucky trajectory. Add the existing equal-weight and cash baselines with identical dates and account conditions.
- Training: both arms restore round 36 `last.zip` and its optimizer; its hash equals `policy.zip`. A uses learning rate 0.0003; B uses 0.00003. Run exactly 50 sessions of 512 decisions per arm, validating each session. Keep the original save/load session semantics and seed the window sampler with 42 + parent timesteps.
- Use CUDA for both training arms, as in the source experiment. Evaluations may use CPU, but the paired training validations use the same device. Verify initial validation against round 36's saved metrics.
- Main comparison: median of defined Sharpe values in each arm's final 10 evaluations, with the defined count and no-trade count always adjacent; if none are defined, report null, not zero. Inspect their return, cash and drawdown trajectories. Secondary comparison: best validation Sharpe versus the incumbent 0.7717247071642092.
- Stop after the fixed budget. Cash reduction alone is not success. This is one training seed on reused development validation data; neither stochastic repetitions nor 50 sessions establish general out-of-sample superiority.
- Full artifacts live under `outputs/ppo-stagnation-20261008`; commit scripts, tests, compact numeric evidence and the report under `docs/research/2026-10-08-ppo-stagnation`.

## Review focus

- Loading `policy.zip` at rounds 300/600 would silently evaluate round 36 again: verify checkpoint path and hash.
- `PPO.load` resets RNG: reseed stochastic evaluation after load and record every seed.
- A learning-rate attribute change can leave a stale SB3 schedule: test the actual optimizer rate after an update, retaining optimizer moments.
- Undefined Sharpe must remain null and no-trade cases must remain visible.
- Experimental outputs must not overwrite source checkpoints or become ordinary model versions. Verify source hashes before and after.

## Tasks

- [ ] Implement and test `scripts/diagnose_policy_stagnation.py` with `tests/test_policy_stagnation_diagnostics.py`: deterministic/random action selection, action trace, null-preserving summaries, and real synthetic-account replay.
- [ ] Implement and test `scripts/run_policy_lr_trial.py` with `tests/test_policy_lr_trial.py`: load optimizer and weights, apply actual learning rate, advance exact budget, keep paired sampling provenance, and preserve source artifacts.
- [ ] Independently review scripts, run applicable existing evaluator/continuation tests, and commit verified research tooling.
- [ ] Run the fixed diagnosis and both training arms; verify counts, hashes, initial metrics, timestep totals and paired seeds.
- [ ] Commit compact evidence and a report distinguishing observations, hypotheses and limitations.

## Execution notes

The existing untracked `docs/planning-artifacts/prd.md` is unrelated and excluded from all commits. Work remains in the existing research workspace so source datasets and the installed environment can be reused. No product feature or model API is changed.
