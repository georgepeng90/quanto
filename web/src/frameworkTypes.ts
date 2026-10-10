import type { ExecutionSpec, MetricSpec, RiskSpec } from './types'
import type { PolicyBacktestSpec, PolicyTrainingSpec } from './investingTypes'

export type InstrumentId = { exchange: string; instrument_type: 'etf' | 'stock' | 'index'; code: string }
export type TradableInstrumentId = InstrumentId & { instrument_type: 'etf' | 'stock' }
export type IndexInstrumentId = InstrumentId & { instrument_type: 'index' }
export const instrumentKey = (instrument: InstrumentId) => `${instrument.exchange}:${instrument.instrument_type}:${instrument.code}`
export type BasketRevisionRef = { basket_id: string; revision: number }
export type FrozenBasketRevision = BasketRevisionRef & { members: TradableInstrumentId[]; created_at: string; selection_note: string; content_hash: string }
export type BasketSummary = { basket_id: string; name: string; current_revision: number; created_at: string }
export type BasketDetail = BasketSummary & { current: FrozenBasketRevision; revisions: { revision: number; content_hash: string; created_at: string }[] }
export type BasketCreateRequest = { name: string; members: TradableInstrumentId[]; selection_note: string; idempotency_key: string }
export type BasketRevisionRequest = { expected_revision: number; members: TradableInstrumentId[]; selection_note: string }
export type DefinitionRef = { kind: 'feature' | 'learning' | 'trading'; id: string; version: string }
export type LearningDefinitionRef = DefinitionRef & { kind: 'learning' }
export type TradingDefinitionRef = DefinitionRef & { kind: 'trading' }
export type Definition = { ref: DefinitionRef; implementation_hash: string; definition_hash: string; supported_instrument_types: InstrumentId['instrument_type'][]; parameter_schema: Record<string, unknown>; [key: string]: unknown }
export type FrameworkErrorCode = 'IDENTITY_UNRESOLVED' | 'BINDING_MISMATCH' | 'MIXED_BASKET_UNSUPPORTED' | 'DEFINITION_UNAVAILABLE' | 'DATA_REQUIREMENT_UNSATISFIED' | 'TEMPORAL_EVIDENCE_MISSING' | 'EXECUTION_CAPABILITY_MISSING' | 'MODEL_IDENTITY_MISMATCH'
export type CapabilityCheck = { name: string; status: 'passed' | 'blocked'; field_path: string; code: FrameworkErrorCode | null; message: string }
export type CapabilityReport = { operation: 'fetch' | 'fixed_backtest' | 'train' | 'resume' | 'policy_backtest' | 'bind_model'; status: 'ready' | 'blocked'; request_hash: string | null; dataset_version_id: string | null; input_hashes: Record<string, unknown>; checks: CapabilityCheck[]; assumptions: string[]; limitations: string[]; checker_version: string; generated_at: string | null }
export type AssetMapping = { instrument_id: TradableInstrumentId; local_symbol: string; evidence_ref: string }
export type AssetBinding = { schema_version: 1; dataset_version_id: string; dataset_content_hash: string; assets: AssetMapping[]; benchmark: { instrument_id: InstrumentId | null; custom_series_id: string | null; return_basis: 'price_index' | 'total_return' | 'other'; evidence_ref: string }; table_semantics: { table: string; semantic_id: string; version: string; schema_definition: Record<string, unknown>; temporal_rules: Record<string, unknown>; evidence_ref: string; declaration_hash: string }[]; binding_hash: string }
export type ResearchInterval = { start: string; end: string; warmup_start: string }
export type RequirementField = { name: string; dtype: 'float' | 'int' | 'bool' | 'string' | 'date' | 'datetime'; unit: string; currency: string | null; report_basis: string | null }
export type RequirementItem = { requirement_id: string; table: string; semantic_version: string; fields: RequirementField[]; scope: 'basket' | 'reference'; reference_ids: InstrumentId[]; interval: ResearchInterval; warmup_sessions: number; availability_rule: string; price_modes: ('raw_cash' | 'adjusted_research')[]; required: boolean; missing_policy: 'reject' | 'mask'; accepted_limitations: string[] }
export type ResolvedDefinition = { ref: DefinitionRef; implementation_hash: string; effective_params: Record<string, unknown>; declaration: Record<string, unknown> }
export type ResolvedConfiguration = { framework_contract_version: 'multi-asset-v1'; operation: 'fixed_backtest' | 'train' | 'resume' | 'policy_backtest'; request: Record<string, unknown>; basket: FrozenBasketRevision; asset_binding: AssetBinding; ordered_pairs: AssetMapping[]; definitions: ResolvedDefinition[]; requirements: RequirementItem[]; internal_backtest_spec: Record<string, unknown>; internal_training_spec: Record<string, unknown> | null; request_hash: string; capability_report: CapabilityReport }
export type DatasetBindingRequest = { evidence_ref: string; idempotency_key: string }
export type ModelBindingRequest = { idempotency_key: string }
export type FetchRequest = { instruments: TradableInstrumentId[]; start_date: string; end_date: string; historical_start?: string | null; price_mode?: 'raw_cash' | 'adjusted_research'; benchmark?: IndexInstrumentId; auxiliary?: Record<string, unknown> }
export type FetchJobRequest = FetchRequest & { refresh?: boolean }
export type BacktestInput = { start: string; end: string; initial_cash: number; seed: number; execution: ExecutionSpec; risk: RiskSpec; metrics: MetricSpec }
export type FixedRequest = { framework_contract_version: 'multi-asset-v1'; basket_revision_ref: BasketRevisionRef; dataset_version_id: string; strategy_revision_id: string; trading_definition_ref: TradingDefinitionRef; strategy_params: Record<string, unknown>; backtest_spec: BacktestInput; sample_purpose: string; source_run_id?: string | null }
type TrainingRequired = 'name' | 'train_start' | 'train_end' | 'validation_start' | 'validation_end'
export type TrainingInput = Pick<PolicyTrainingSpec, TrainingRequired> & Partial<Omit<PolicyTrainingSpec, TrainingRequired | 'backtest'>> & { backtest: BacktestInput }
export type ModelCreateRequest = { framework_contract_version: 'multi-asset-v1'; basket_revision_ref: BasketRevisionRef; dataset_id: string; learning_definition_ref: LearningDefinitionRef; trading_definition_ref: TradingDefinitionRef; spec: TrainingInput }
export type ResumeRequest = { model_id: string; additional_timesteps?: number | null; device?: 'cpu' | 'cuda' | null }
export type PolicyBacktestInput = Pick<PolicyBacktestSpec, 'start' | 'end'> & { evaluation_purpose: 'development_replay' | 'post_selection_backtest'; baseline?: PolicyBacktestSpec['baseline']; cost_multiplier?: number }
export type PolicyBacktestRequest = { version_id: string; dataset_id: string; spec: PolicyBacktestInput }
export type CapabilityCheckRequest =
  | ({ operation: 'fetch' } & FetchRequest)
  | ({ operation: 'fixed_backtest' } & FixedRequest)
  | ({ operation: 'train' } & ModelCreateRequest)
  | ({ operation: 'resume' } & ResumeRequest)
  | ({ operation: 'policy_backtest'; model_id: string } & PolicyBacktestRequest)
export type ModelFrameworkBinding = { schema_version: 1; model_id: string; origin: 'new' | 'legacy_audited'; original_spec: Record<string, unknown>; original_spec_hash: string; training_configuration: ResolvedConfiguration; semantic_identity: Record<string, unknown>; evidence: Record<string, unknown>; created_at: string; binding_hash: string }
