import type { DatasetVersion, ExperimentDraft, ExperimentPayload, JobRecord, Page, Preflight, StrategyManifest, StrategyRevision } from './types'

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
  const response = await fetch(`${current.api_base}${path}`, { ...init, headers: { Authorization: `Bearer ${current.token}`, ...init.headers } })
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { code?: string; message?: string; details?: Record<string, unknown> } | null
    throw new ApiError(body?.code || 'HTTP_ERROR', body?.message || '请求无法完成', body?.details || {}, response.status)
  }
  return response.json() as Promise<T>
}

const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const list = <T>(name: string, cursor?: string) => request<Page<T>>(`/${name}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)

export const api = {
  datasets: (cursor?: string) => list<DatasetVersion>('datasets', cursor),
  dataset: (id: string) => request<DatasetVersion>(`/datasets/${encodeURIComponent(id)}`),
  strategies: (cursor?: string) => list<StrategyRevision>('strategies', cursor),
  strategy: (id: string) => request<StrategyRevision>(`/strategies/${encodeURIComponent(id)}`),
  registerStrategy: (manifest: StrategyManifest, files: Record<string, string>) => request<StrategyRevision>('/strategies', json({ manifest, files })),
  experiments: (cursor?: string) => list<ExperimentDraft>('experiments', cursor),
  experiment: (id: string) => request<ExperimentDraft>(`/experiments/${encodeURIComponent(id)}`),
  createExperiment: (payload: ExperimentPayload) => request<ExperimentDraft>('/experiments', json({ payload })),
  saveExperiment: (id: string, expected_revision: number, payload: ExperimentPayload) => request<ExperimentDraft>(`/experiments/${encodeURIComponent(id)}`, { ...json({ expected_revision, payload }), method: 'PUT' }),
  jobs: (cursor?: string) => list<JobRecord>('jobs', cursor),
  job: (id: string) => request<JobRecord>(`/jobs/${encodeURIComponent(id)}`),
  dataJob: (kind: 'demo' | 'fetch', payload: Record<string, unknown>) => request<JobRecord>('/jobs/data', json({ kind, payload })),
  validateStrategy: (strategy_revision_id: string, params: Record<string, unknown>) => request<JobRecord>('/jobs/strategy-validation', json({ strategy_revision_id, params })),
  upload: (file: File) => { const body = new FormData(); body.append('file', file); return request<{ upload_id: string }>('/uploads', { method: 'POST', body }) },
  preflight: (upload_id: string) => request<Preflight>('/datasets/preflight', json({ upload_id })),
  importDataset: (upload_id: string) => request<JobRecord>('/datasets', json({ upload_id }))
}

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : '未知错误'
  const detail = error.details.reason || (Array.isArray(error.details.errors) ? JSON.stringify(error.details.errors) : '')
  const location = error.code === 'SYNTAX_ERROR' ? ` ${String(error.details.file || '')}:${String(error.details.line || '')}` : ''
  return `${error.message}${location}${detail ? ` · ${detail}` : ''} (${error.code})`
}
