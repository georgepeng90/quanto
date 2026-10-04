import type { BacktestSpec, DatasetVersion, JobRecord } from './types'
import type { LearningChart, LearningOverview, LearningTablePage } from './learningTypes'

export type InvestingArtifact = { id: string; kind: 'policy' | 'policy_backtest'; created_at: string; job_id: string; state: 'succeeded'; manifest: Record<string, unknown> }
export type InvestingOverview = { capabilities: LearningOverview['capabilities'] & { ppo_available?: boolean; gymnasium_available?: boolean }; datasets: DatasetVersion[]; policies: InvestingArtifact[]; backtests: InvestingArtifact[]; jobs: JobRecord[] }
export type PolicyTrainingSpec = { name: string; backtest: BacktestSpec; frequency: 'weekly' | 'monthly'; lookback: number; train_start: string; train_end: string; validation_start: string; validation_end: string; test_start: string; test_end: string; episode_sessions: number; total_timesteps: number; n_steps: number; batch_size: number; n_epochs: number; learning_rate: number; gamma: number; gae_lambda: number; ent_coef: number; hidden_size: number; seed: number; device: 'cpu' | 'cuda'; eval_every_timesteps: number; selection_metric: 'validation_net_log_return' | 'validation_sharpe' }
export type PolicyBacktestSpec = { start: string; end: string; baseline: 'policy' | 'cash' | 'equal_weight' | 'buy_hold'; cost_multiplier: number }
export type InvestingChart = LearningChart
export type InvestingTablePage = LearningTablePage

export type InvestingModel = { id: string; name: string; dataset_id: string; spec: PolicyTrainingSpec; created_at: string; state: string; active_job_id: string | null; session_count: number; version_count: number; total_timesteps: number; latest_version_id: string | null }
export type InvestingModelDetail = InvestingModel & { sessions: (JobRecord & { base_version_id: string | null })[]; versions: InvestingArtifact[] }
