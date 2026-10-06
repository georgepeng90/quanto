import type { BacktestSpec, DatasetVersion, JobRecord } from './types'
import type { LearningChart, LearningOverview, LearningTablePage } from './learningTypes'

export type InvestingArtifact = { id: string; kind: 'policy' | 'policy_backtest'; created_at: string; job_id: string; state: 'succeeded'; manifest: Record<string, unknown> }
export type InvestingOverview = { capabilities: LearningOverview['capabilities'] & { ppo_available?: boolean; gymnasium_available?: boolean }; datasets: DatasetVersion[]; policies: InvestingArtifact[]; backtests: InvestingArtifact[]; jobs: JobRecord[] }
export type RewardVersion = 'net-log-return-v1' | 'net-log-return-with-state-v1' | 'differential-sharpe-v1'
export type PolicyTrainingSpec = { name: string; backtest: BacktestSpec; frequency: 'weekly' | 'monthly'; action_version?: 'hold-target-quartiles-v1' | 'hold-target-cash-quartiles-v2'; reward_version?: RewardVersion; lookback: number; train_start: string; train_end: string; validation_start: string; validation_end: string; test_start?: string | null; test_end?: string | null; episode_sessions: number; total_timesteps: number; n_steps: number; batch_size: number; n_epochs: number; learning_rate: number; gamma: number; gae_lambda: number; ent_coef: number; hidden_size: number; seed: number; device: 'cpu' | 'cuda'; eval_every_timesteps: number; selection_metric: 'validation_net_log_return' | 'validation_sharpe' }
export type PolicyBacktestSpec = { start: string; end: string; baseline: 'policy' | 'cash' | 'equal_weight' | 'buy_hold'; cost_multiplier: number }
export type InvestingChart = LearningChart
export type InvestingTablePage = LearningTablePage

export type InvestingModel = { id: string; name: string; dataset_id: string; spec: PolicyTrainingSpec; created_at: string; state: string; active_job_id: string | null; session_count: number; version_count: number; total_timesteps: number; latest_version_id: string | null }
export type InvestingModelDetail = InvestingModel & { sessions: (JobRecord & { base_version_id: string | null })[]; versions: (InvestingArtifact & { round_number: number; best_round_number: number | null })[] }
export type TrainingHistoryItem = {
  id: string; session_number: number; round_number: number | null; base_version_id: string | null; base_round_number: number | null
  state: string; created_at: string | null; started_at: string | null; finished_at: string | null; heartbeat: string | null
  completed_through: string | null; error: string | null; saved_at?: string | null; timesteps_completed?: number; session_timesteps?: number
  best_timesteps?: number; best_round_number?: number | null; best_source_version_id?: string | null; runtime_device?: string | null
  selection_valid?: boolean; validation_latest?: Record<string, unknown>; validation_best?: Record<string, unknown>
}
export type InvestingModelSummary = InvestingModel & { active_session: TrainingHistoryItem | null; capabilities?: { cuda_available: boolean } }
export type TrainingHistoryPage = { items: TrainingHistoryItem[]; total: number; page: number; page_size: number; total_pages: number; anchor: number }
export type TrainingHistoryQuery = {
  kind: 'versions' | 'sessions'; page: number; page_size: number; anchor?: number
  round_number?: number; session_number?: number
}
