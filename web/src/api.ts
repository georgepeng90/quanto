import type { InvestingModel, InvestingModelDetail, InvestingModelSummary, TrainingHistoryPage, TrainingHistoryQuery, InvestingArtifact, InvestingChart, InvestingOverview, InvestingTablePage } from './investingTypes'
import type { BacktestCatalogRow, ChartData, ChartName, ChartQuery, ComparisonResult, DatasetVersion, ExperimentDraft, ExperimentPayload, JobRecord, LogPage, Page, Preflight, RunRecord, RunResult, StrategyManifest, StrategyRevision, TableName, TablePage, TableQuery } from './types'
import type { LearningArtifact, LearningChart, LearningOverview, LearningTablePage } from './learningTypes'
import type { ScreeningRecord, ScreeningSummary } from './types'
import type { BasketCreateRequest, BasketDetail, BasketRevisionRequest, BasketSummary, CapabilityCheckRequest, CapabilityReport, Definition, FrozenBasketRevision, FetchJobRequest, ModelCreateRequest, ResumeRequest, PolicyBacktestRequest, PolicyBacktestPreview } from './frameworkTypes'

export class ApiError extends Error {
  constructor(public code: string, message: string, public details: Record<string, unknown> = {}, public status = 0) { super(message) }
}

let sessionPromise: Promise<{ token: string; api_base: string; timezone: string }> | null = null

async function session() {
  if (!sessionPromise) sessionPromise = fetch('/api/session').then(async response => {
    if (!response.ok) throw new ApiError('SESSION_ERROR', '无法建立本地会话', {}, response.status)
    return response.json()
  }).catch(error => { sessionPromise = null; throw error })
  return sessionPromise
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const current = await session()
  init.signal?.throwIfAborted()
  const response = await fetch(`${current.api_base}${path}`, { ...init, headers: { Authorization: `Bearer ${current.token}`, ...init.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { code?: string; message?: string; details?: Record<string, unknown> } | null
    throw new ApiError(body?.code || 'HTTP_ERROR', body?.message || '请求无法完成', body?.details || {}, response.status)
  }
  return response.json() as Promise<T>
}

async function resultRequest<T>(path: string, signal?: AbortSignal): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    try { return await request<T>(path, { signal }) }
    catch (value) {
      const transient = !(value instanceof ApiError) || value.status === 408 || value.status === 429 || value.status >= 500
      if (signal?.aborted || !transient || attempt >= 2) throw value
      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(signal?.reason) }
        const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve() }, 300 * (attempt + 1))
        signal?.addEventListener('abort', abort, { once: true })
        if (signal?.aborted) abort()
      })
    }
  }
}

const queryString = (query: Record<string, string | number | undefined>) => {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== '') params.set(key, String(value))
  return params.size ? `?${params}` : ''
}

async function download(path: string, signal?: AbortSignal): Promise<Blob> {
  const current = await session()
  const response = await fetch(`${current.api_base}${path}`, { headers: { Authorization: `Bearer ${current.token}` }, signal })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { code?: string; message?: string; details?: Record<string, unknown> } | null
    throw new ApiError(body?.code || 'HTTP_ERROR', body?.message || '下载无法完成', body?.details || {}, response.status)
  }
  return response.blob()
}

const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const list = <T>(name: string, cursor?: string) => request<Page<T>>(`/${name}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)

export const api = {
  screenings: (cursor?: string, signal?: AbortSignal) => request<Page<ScreeningSummary>>(`/screenings${queryString({ cursor })}`, { signal }),
  screening: (id: string, signal?: AbortSignal) => request<ScreeningRecord>(`/screenings/${encodeURIComponent(id)}`, { signal }),
  useScreening: (id: string, name: string, key: string) => request<FrozenBasketRevision>(`/screenings/${encodeURIComponent(id)}/basket`, json({ name, idempotency_key: key })),
  exportScreening: (id: string) => download(`/screenings/${encodeURIComponent(id)}/export`),
  listBaskets: (cursor?: string) => list<BasketSummary>('baskets', cursor),
  getBasket: (id: string) => request<BasketDetail>(`/baskets/${encodeURIComponent(id)}`),
  createBasket: (body: BasketCreateRequest) => request<FrozenBasketRevision>('/baskets', json(body)),
  reviseBasket: (id: string, body: BasketRevisionRequest) => request<FrozenBasketRevision>(`/baskets/${encodeURIComponent(id)}/revisions`, json(body)),
  getBasketRevision: (id: string, revision: number) => request<FrozenBasketRevision>(`/baskets/${encodeURIComponent(id)}/revisions/${revision}`),
  listDefinitions: () => request<{ items: Definition[] }>('/definitions'),
  checkCapabilities: (body: CapabilityCheckRequest) => request<CapabilityReport>('/capabilities/check', json(body)),
  investingModels: (cursor?: string, signal?: AbortSignal) => request<Page<InvestingModel>>(`/investing/models${queryString({ cursor })}`, { signal }),
  investingModel: (id: string, signal?: AbortSignal) => request<InvestingModelDetail>(`/investing/models/${encodeURIComponent(id)}`, { signal }),
  investingModelSummary: (id: string, signal?: AbortSignal) => request<InvestingModelSummary>(`/investing/models/${encodeURIComponent(id)}?include_history=false`, { signal }),
  investingModelHistory: (id: string, query: TrainingHistoryQuery, signal?: AbortSignal) => request<TrainingHistoryPage>(`/investing/models/${encodeURIComponent(id)}/history${queryString(query)}`, { signal }),
  createInvestingModel: (body: ModelCreateRequest) => request<InvestingModel>('/investing/models', json(body)),
  trainInvestingModel: (id: string, body: Omit<ResumeRequest, 'model_id'>) => request<JobRecord>(`/investing/models/${encodeURIComponent(id)}/train`, json(body)),
  previewInvestingModelBacktest: (id: string, body: PolicyBacktestRequest, signal?: AbortSignal) => request<PolicyBacktestPreview>(`/investing/models/${encodeURIComponent(id)}/backtests/preview`, { ...json(body), signal }),
  backtestInvestingModel: (id: string, body: PolicyBacktestRequest, signal?: AbortSignal) => request<JobRecord>(`/investing/models/${encodeURIComponent(id)}/backtests`, { ...json(body), signal }),
  backtestCatalog: (cursor?: string, signal?: AbortSignal) => request<Page<BacktestCatalogRow>>(`/backtests/catalog${queryString({ cursor, limit: 50 })}`, { signal }),
  investingOverview: (signal?: AbortSignal) => request<InvestingOverview>('/investing/overview', { signal }),
  investingArtifact: (resource: 'policies' | 'backtests', id: string, signal?: AbortSignal) => request<InvestingArtifact>(`/investing/${resource}/${encodeURIComponent(id)}`, { signal }),
  investingChart: (id: string, signal?: AbortSignal) => request<InvestingChart>(`/investing/backtests/${encodeURIComponent(id)}/charts?max_points=1000`, { signal }),
  investingTable: (id: string, name: string, cursor: number | string = 0, signal?: AbortSignal) => request<InvestingTablePage>(`/investing/backtests/${encodeURIComponent(id)}/tables/${encodeURIComponent(name)}?cursor=${encodeURIComponent(cursor)}&limit=100`, { signal }),
  exportInvesting: (id: string, signal?: AbortSignal) => download(`/investing/backtests/${encodeURIComponent(id)}/export`, signal),
  learningOverview: (signal?: AbortSignal) => request<LearningOverview>('/learning/overview', { signal }),
  learningArtifacts: (resource: 'datasets' | 'models' | 'backtests', cursor: number | string = 0, signal?: AbortSignal) => request<{ items: LearningArtifact[]; next_cursor: number | string | null }>(`/learning/${resource}?cursor=${encodeURIComponent(cursor)}&limit=50`, { signal }),
  learningArtifact: (resource: 'datasets' | 'models' | 'backtests', id: string, signal?: AbortSignal) => request<LearningArtifact>(`/learning/${resource}/${encodeURIComponent(id)}`, { signal }),
  learningChart: (id: string, signal?: AbortSignal) => request<LearningChart>(`/learning/backtests/${encodeURIComponent(id)}/charts?max_points=1000`, { signal }),
  learningTable: (id: string, name: string, cursor: number | string = 0, signal?: AbortSignal) => request<LearningTablePage>(`/learning/backtests/${encodeURIComponent(id)}/tables/${encodeURIComponent(name)}?cursor=${encodeURIComponent(cursor)}&limit=100`, { signal }),
  exportLearning: (id: string, signal?: AbortSignal) => download(`/learning/backtests/${encodeURIComponent(id)}/export`, signal),
  jobLogs: (id: string, cursor?: string, signal?: AbortSignal) => request<LogPage>(`/jobs/${encodeURIComponent(id)}/logs${queryString({ cursor })}`, { signal }),
  cancelJob: (id: string) => request<JobRecord>(`/jobs/${encodeURIComponent(id)}/cancel`, json({})),
  datasets: (cursor?: string) => list<DatasetVersion>('datasets', cursor),
  dataset: (id: string, signal?: AbortSignal) => request<DatasetVersion>(`/datasets/${encodeURIComponent(id)}`, { signal }),
  strategies: (cursor?: string) => list<StrategyRevision>('strategies', cursor),
  strategy: (id: string) => request<StrategyRevision>(`/strategies/${encodeURIComponent(id)}`),
  registerStrategy: (manifest: StrategyManifest, files: Record<string, string>) => request<StrategyRevision>('/strategies', json({ manifest, files })),
  experiments: (cursor?: string) => list<ExperimentDraft>('experiments', cursor),
  experiment: (id: string) => request<ExperimentDraft>(`/experiments/${encodeURIComponent(id)}`),
  createExperiment: (payload: ExperimentPayload) => request<ExperimentDraft>('/experiments', json({ payload })),
  saveExperiment: (id: string, expected_revision: number, payload: ExperimentPayload) => request<ExperimentDraft>(`/experiments/${encodeURIComponent(id)}`, { ...json({ expected_revision, payload }), method: 'PUT' }),
  jobs: (cursor?: string) => list<JobRecord>('jobs', cursor),
  job: (id: string) => request<JobRecord>(`/jobs/${encodeURIComponent(id)}`),
  dataJob: (kind: 'fetch', payload: FetchJobRequest) => request<JobRecord>('/jobs/data', json({ kind, payload })),
  validateStrategy: (strategy_revision_id: string, params: Record<string, unknown>) => request<JobRecord>('/jobs/strategy-validation', json({ strategy_revision_id, params })),
  upload: (file: File) => { const body = new FormData(); body.append('file', file); return request<{ upload_id: string }>('/uploads', { method: 'POST', body }) },
  preflight: (upload_id: string) => request<Preflight>('/datasets/preflight', json({ upload_id })),
  importDataset: (upload_id: string) => request<JobRecord>('/datasets', json({ upload_id })),
  runs: (cursor?: string, signal?: AbortSignal) => request<Page<RunRecord>>(`/runs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`, { signal }),
  run: (id: string, signal?: AbortSignal) => request<RunRecord>(`/runs/${encodeURIComponent(id)}`, { signal }),
  submitRun: (experiment_id: string, expected_revision: number, idempotency_key: string) => request<RunRecord>('/runs', json({ experiment_id, expected_revision, idempotency_key, source: 'web' })),
  submitBatch: (items: { experiment_id: string; expected_revision: number }[], idempotency_key: string) => request<{ items: RunRecord[] }>('/runs/batch', json({ items, idempotency_key, source: 'web' })),
  cloneRun: (id: string) => request<ExperimentDraft>(`/runs/${encodeURIComponent(id)}/clone`, json({})),
  cancelRun: (id: string) => request<RunRecord>(`/runs/${encodeURIComponent(id)}/cancel`, json({})),
  runLogs: (id: string, cursor?: string, signal?: AbortSignal, tail = false) => request<LogPage>(`/runs/${encodeURIComponent(id)}/logs${queryString({ cursor, tail: tail ? 'true' : undefined })}`, { signal }),
  runTable: (id: string, name: TableName, query: TableQuery = {}, signal?: AbortSignal) => resultRequest<TablePage>(`/runs/${encodeURIComponent(id)}/tables/${name}${queryString(query)}`, signal),
  runChart: (id: string, name: ChartName, query: ChartQuery = {}, signal?: AbortSignal) => resultRequest<ChartData>(`/runs/${encodeURIComponent(id)}/charts/${name}${queryString(query)}`, signal),
  runResult: (id: string, signal?: AbortSignal) => request<RunResult>(`/runs/${encodeURIComponent(id)}/results`, { signal }),
  comparison: (run_ids: string[], signal?: AbortSignal) => request<ComparisonResult>('/comparisons', { ...json({ run_ids }), signal }),
  exportRun: (id: string, format: 'csv' | 'json' | 'html', signal?: AbortSignal) => download(`/runs/${encodeURIComponent(id)}/exports/${format}`, signal)
}

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : '未知错误'
  const detail = error.details.reason || (Array.isArray(error.details.errors) ? JSON.stringify(error.details.errors) : '')
  const location = error.code === 'SYNTAX_ERROR' ? ` ${String(error.details.file || '')}:${String(error.details.line || '')}` : ''
  return `${error.message}${location}${detail ? ` · ${detail}` : ''} (${error.code})`
}
