export type Page<T> = { items: T[]; next_cursor: string | null }
export type ParameterSchema = { type: string; title?: string; description?: string; default?: unknown; enum?: unknown[]; properties?: Record<string, ParameterSchema>; items?: ParameterSchema; required?: string[]; minimum?: number; maximum?: number }
export type DatasetManifest = {
  source: string; is_demo: boolean; price_mode: 'raw_cash' | 'adjusted_research'; currency?: string
  calendar_start: string; calendar_end: string; data_start: string; data_end: string
  event_coverage: 'complete' | 'partial' | 'unknown'; benchmark_type?: string; limitations: string[]
  auxiliary_tables?: Record<string, string>; universe_rule?: Record<string, unknown>
}
export type DatasetVersion = { id: string; content_hash: string; manifest: DatasetManifest; instruments?: { symbol: string; lot_size: number; settlement_sessions: number; listing_date: string; group: string }[] }
export type StrategyManifest = { entrypoint: string; parameter_schema: ParameterSchema; auxiliary_sources: string[]; research_data: string[]; [key: string]: unknown }
export type StrategyRevision = { id: string; content_hash: string; manifest: StrategyManifest; files?: Record<string, string> }
export type ExecutionSpec = { commission_rate: number; minimum_commission: number; slippage_rate: number; max_stale_sessions: number; model: 'next_open' }
export type StressScenario = { name: string; symbol_shocks: Record<string, number>; execution_cost_rate: number }
export type RiskSpec = { max_total_weight: number; max_symbol_weight: number; group_caps: Record<string, number>; stress_scenarios: StressScenario[]; max_stress_loss?: number | null; drawdown_pause?: number | null; pause_action: 'block_new' | 'reduce' | 'liquidate'; pause_target_weight?: number | null; recovery_mode: 'never' | 'recorded_approval' }
export type MetricSpec = { annual_sessions: number; risk_free_rate: number; cash_rate: number; definition_version: string }
export type BacktestSpec = { start: string; end: string; symbols: string[]; initial_cash: number; seed: number; execution: ExecutionSpec; risk: RiskSpec; metrics: MetricSpec }
export type ExperimentPayload = { source_run_id?: string; strategy_revision_id: string; dataset_version_id: string; strategy_params: Record<string, unknown>; effective_strategy_params?: Record<string, unknown>; backtest_spec: BacktestSpec; sample_purpose: string }
export type ExperimentDraft = { id: string; revision: number; payload: ExperimentPayload }
export type JobResult = { state?: 'complete' | 'incomplete'; dataset_version_id?: string | null; market_prefetch_state?: 'queued' | 'running' | 'ready' | 'incomplete' | 'failed' | 'cancelled'; market_snapshot_ref?: { id: string; manifest_digest: string } | null; coverage?: Record<string, unknown>; price_mode?: DatasetManifest['price_mode']; limitations?: string[]; failed_symbols?: string[]; reason?: string; valid?: boolean; warmup_bars?: number | null }
export type JobRecord = { id: string; kind: 'backtest' | 'demo' | 'fetch' | 'import' | 'validate_strategy'; state: string; run_id: string | null; enqueue_seq: number; heartbeat: string | null; completed_through: string | null; error: string | null; created_at: string | null; started_at: string | null; finished_at: string | null; result?: JobResult }
export type Preflight = Pick<DatasetManifest, 'price_mode' | 'is_demo' | 'event_coverage' | 'calendar_start' | 'calendar_end' | 'data_start' | 'data_end' | 'limitations'> & { missing_tables: string[]; validation: 'metadata_only' }
export type RunState = 'queued' | 'running' | 'cancel_requested' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'
export type RunRecord = { run_id: string; job_id: string; experiment_id: string; experiment_revision: number; manifest_hash: string; source: string; parent_run_id: string | null; group_id: string | null; state: RunState; completed_through: string | null; error: string | null; created_at: string | null; started_at: string | null; finished_at: string | null; detail_url: string; provenance?: Record<string, unknown> }
export type RunEvent = { seq: number; phase: string; asof: string | null; completed_days: number; total_days: number; level: string; code: string; message: string; payload: Record<string, unknown> }
export type LogPage = Page<RunEvent> & { state: RunState; completed_through: string | null }
export type TableName = 'equity' | 'orders' | 'fills' | 'signals' | 'risk_events' | 'account_events' | 'positions' | 'curves' | 'monthly_returns' | 'annual_returns' | 'matched_trades' | 'open_lots'
export type TableRow = Record<string, unknown>
export type TableLoadState = { loading: boolean; error?: string }
export type TableQuery = { cursor?: string; limit?: number; date_from?: string; date_to?: string; symbol?: string; context_date?: string }
export type TablePage = Page<TableRow> & { total: number }
export type ChartName = 'curves' | 'monthly_returns' | 'annual_returns' | 'allocation'
export type ChartQuery = { date_from?: string; date_to?: string; max_points?: number }
export type ChartData = { items: TableRow[]; total_rows: number; sampled: boolean }
export type MetricDefinition = Record<string, unknown>
export type RunResult = { run_id: string; state: RunState; completed_through: string | null; metrics: Record<string, number | null> | null; metric_definitions: Record<string, MetricDefinition>; warnings?: string[]; metadata?: Record<string, unknown>; error?: string | null; detail_url: string }
export type ComparisonRun = { run_id: string; state: RunState; metrics: Record<string, number | null> | null; metric_definitions: Record<string, MetricDefinition> | null; [key: string]: unknown }
export type ComparisonResult = { runs: ComparisonRun[]; differences: Record<string, unknown[]>; directly_comparable: boolean }
