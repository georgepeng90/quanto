import { beforeEach, expect, test, vi } from 'vitest'
import { api } from '../src/api'
import type { CapabilityCheckRequest, PolicyBacktestRequest, RequirementItem } from '../src/frameworkTypes'

const member = { exchange: 'SSE', instrument_type: 'etf' as const, code: '512480' }
const revision = { basket_id: 'basket 1', revision: 1, members: [member], selection_note: '研究选择', created_at: '2026-10-11T00:00:00Z', content_hash: 'hash' }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/baskets') return Response.json(init?.method === 'POST' ? revision : { items: [], next_cursor: null })
    if (path === '/api/baskets/basket%201') return Response.json({ basket_id: 'basket 1', name: 'ETF', current_revision: 1, created_at: revision.created_at, current: revision, revisions: [{ revision: 1, content_hash: 'hash', created_at: revision.created_at }] })
    if (path.startsWith('/api/baskets/basket%201/revisions')) return Response.json(revision)
    if (path === '/api/definitions') return Response.json({ items: [] })
    if (path === '/api/capabilities/check') return Response.json({ operation: 'fetch', status: 'blocked', request_hash: null, dataset_version_id: null, input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null })
    if (path === '/api/datasets/d%201/asset-binding') return Response.json({ schema_version: 1 })
    if (path === '/api/investing/models/m%201/framework-binding') return Response.json({ schema_version: 1 })
    throw new Error(`unexpected ${path}`)
  }))
})

test('framework API uses authenticated narrow routes and exact body shapes', async () => {
  await api.listBaskets(); await api.getBasket('basket 1')
  await api.createBasket({ name: 'ETF', members: [member], selection_note: '研究选择', idempotency_key: 'create-1' })
  await api.reviseBasket('basket 1', { expected_revision: 1, members: [member], selection_note: '重排' })
  await api.getBasketRevision('basket 1', 1); await api.listDefinitions()
  await api.checkCapabilities({ operation: 'fetch', instruments: [member], benchmark: { exchange: 'SSE', instrument_type: 'index', code: '000300' }, start_date: '2025-01-01', end_date: '2025-01-05' })
  await api.bindDataset('d 1', { evidence_ref: 'market:snapshot', idempotency_key: 'bind-1' })
  await api.bindModel('m 1', { idempotency_key: 'model-1' })
  const calls = vi.mocked(fetch).mock.calls.slice(1).map(([url, init]) => ({ path: String(url), method: init?.method || 'GET', body: init?.body ? JSON.parse(init.body as string) : null, auth: new Headers(init?.headers).get('Authorization') }))
  expect(calls.map(call => [call.method, call.path])).toEqual([
    ['GET', '/api/baskets'], ['GET', '/api/baskets/basket%201'], ['POST', '/api/baskets'], ['POST', '/api/baskets/basket%201/revisions'], ['GET', '/api/baskets/basket%201/revisions/1'], ['GET', '/api/definitions'], ['POST', '/api/capabilities/check'], ['POST', '/api/datasets/d%201/asset-binding'], ['POST', '/api/investing/models/m%201/framework-binding'],
  ])
  expect(calls[2].body.members).toEqual([member])
  expect(calls[3].body).toEqual({ expected_revision: 1, members: [member], selection_note: '重排' })
  expect(calls[6].body).not.toHaveProperty('symbols')
  expect(calls[7].body).toEqual({ evidence_ref: 'market:snapshot', idempotency_key: 'bind-1' })
  expect(calls[8].body).toEqual({ idempotency_key: 'model-1' })
  expect(calls.every(call => call.auth === 'Bearer token')).toBe(true)
})

test('policy backtest submission body excludes URL model ID while capability check includes it', async () => {
  const submission: PolicyBacktestRequest = { version_id: 'saved-version', dataset_id: 'dataset-1', spec: { start: '2025-01-01', end: '2025-01-31', evaluation_purpose: 'post_selection_backtest' } }
  const check: CapabilityCheckRequest = { operation: 'policy_backtest', model_id: 'model-1', ...submission }
  await api.checkCapabilities(check)
  const [, init] = vi.mocked(fetch).mock.calls.find(([url]) => url === '/api/capabilities/check')!
  expect(JSON.parse(init!.body as string)).toEqual({ operation: 'policy_backtest', model_id: 'model-1', version_id: 'saved-version', dataset_id: 'dataset-1', spec: { start: '2025-01-01', end: '2025-01-31', evaluation_purpose: 'post_selection_backtest' } })
  expect(submission).not.toHaveProperty('model_id')
})

test('requirement JSON keeps research interval dates and defaulted response fields', () => {
  const requirement = {
    requirement_id: 'prices', table: 'prices', semantic_version: 'v1',
    fields: [{ name: 'close', dtype: 'float', unit: 'CNY', currency: null, report_basis: null }],
    scope: 'basket', reference_ids: [], interval: { start: '2025-01-02', end: '2025-01-31', warmup_start: '2024-12-01' },
    warmup_sessions: 20, availability_rule: 'session_close', price_modes: ['adjusted_research'], required: true,
    missing_policy: 'reject', accepted_limitations: [],
  } satisfies RequirementItem
  expect(requirement.interval).toEqual({ start: '2025-01-02', end: '2025-01-31', warmup_start: '2024-12-01' })
  expect(requirement.fields[0]).toEqual({ name: 'close', dtype: 'float', unit: 'CNY', currency: null, report_basis: null })
})
