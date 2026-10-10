import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { ModelDetailPage } from '../src/pages/ModelDetailPage'

const report = { operation: 'bind_model', status: 'ready', request_hash: 'hash', dataset_version_id: 'd1',
  input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null }
const spec = { name: '旧模型', backtest: { symbols: ['LOCAL1'] }, frequency: 'weekly', reward_version: 'net-log-return-v1',
  train_start: '2021-01-01', train_end: '2022-12-31', validation_start: '2023-01-01', validation_end: '2023-12-31',
  test_start: null, test_end: null, selection_metric: 'validation_sharpe', total_timesteps: 4096, device: 'cpu' }
const model = { id: 'm1', name: '旧模型', dataset_id: 'd1', spec, state: 'untrained', active_job_id: null,
  active_session: null, capabilities: { cuda_available: false }, session_count: 0, version_count: 0,
  total_timesteps: 0, latest_version_id: null, created_at: '2024-01-01T00:00:00Z',
  framework_binding: null, framework_binding_check: report }
let bound = false
const posts: { path: string; body: any }[] = []
const renderPage = () => render(<RouterProvider router={createMemoryRouter([
  { path: '/models/:id', element: <ModelDetailPage /> }], { initialEntries: ['/models/m1'] })} />)

beforeEach(() => {
  bound = false; posts.length = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    if (init?.method === 'POST') {
      posts.push({ path, body: JSON.parse(String(init.body)) })
      if (path.endsWith('/framework-binding')) {
        bound = true
        return Response.json({ model_id: 'm1', origin: 'legacy_audited' })
      }
      throw new Error(`Unexpected write ${path}`)
    }
    if (path === '/api/investing/models/m1?include_history=false') return Response.json(bound ? { ...model,
      framework_binding: { model_id: 'm1', origin: 'legacy_audited', binding_hash: 'bound', original_spec_hash: 'original',
        basket_revision_ref: { basket_id: 'b1', revision: 1 }, ordered_pairs: [{ instrument_id: { exchange: 'SZSE', instrument_type: 'etf', code: '000001' }, local_symbol: 'LOCAL1', evidence_ref: 'e1' }], definitions: [], evidence: { audit: 'verified' } }, framework_binding_check: null } : model)
    if (path.startsWith('/api/investing/models/m1/history?')) return Response.json({ items: [], total: 0, page: 1, page_size: 10, total_pages: 0, anchor: 0 })
    if (path === '/api/investing/overview') return Response.json({ capabilities: { cuda_available: false }, datasets: [], policies: [], backtests: [], jobs: [] })
    throw new Error(`Unexpected ${path}`)
  }))
})

test('unbound historical model stays read only until explicit audited association', async () => {
  renderPage()
  expect(await screen.findByText(/尚无可证明的框架身份/)).toBeVisible()
  expect(screen.getByRole('button', { name: '检查续训能力' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '开始训练' })).toBeDisabled()
  expect(screen.queryByLabelText('标的篮子')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '核验并关联旧模型' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].path).toBe('/api/investing/models/m1/framework-binding')
  expect(posts[0].body).toEqual({ idempotency_key: expect.any(String) })
  expect(await screen.findByText(/旧模型审计关联/)).toBeVisible()
})

test('blocked historical audit never offers a writable model operation', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/investing/models/m1?include_history=false') return Response.json({ ...model,
      framework_binding_check: { ...report, status: 'blocked', checks: [{ name: 'identity', status: 'blocked',
        field_path: 'model_id', code: 'IDENTITY_UNRESOLVED', message: 'Evidence missing' }] } })
    return prior(input, init)
  }))
  renderPage()
  expect(await screen.findByText(/Evidence missing/)).toBeVisible()
  expect(screen.getByRole('button', { name: '核验并关联旧模型' })).toBeDisabled()
  expect(posts).toHaveLength(0)
})

test('server conflict replaces a prior ready training report with its blocking report', async () => {
  const prior = globalThis.fetch
  const associated = { ...model, framework_binding: { model_id: 'm1', origin: 'new', binding_hash: 'bound', original_spec_hash: 'original',
    basket_revision_ref: { basket_id: 'b1', revision: 1 }, ordered_pairs: [{ instrument_id: { exchange: 'SZSE', instrument_type: 'etf', code: '000001' }, local_symbol: 'LOCAL1', evidence_ref: 'e1' }],
    definitions: [], evidence: { source: 'new' } }, framework_binding_check: null }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/investing/models/m1?include_history=false') return Response.json(associated)
    if (path === '/api/capabilities/check') return Response.json({ ...report, operation: 'resume' })
    if (path === '/api/investing/models/m1/train' && init?.method === 'POST')
      return Response.json({ code: 'FRAMEWORK_CONFLICT', message: 'Model changed before submission',
        details: { report: { ...report, operation: 'resume', status: 'blocked', checks: [{ name: 'model', status: 'blocked',
          field_path: 'model_id', code: 'MODEL_IDENTITY_MISMATCH', message: 'Binding changed' }] } } }, { status: 409 })
    return prior(input, init)
  }))
  renderPage()
  await screen.findByText(/SZSE:etf:000001/)
  await userEvent.click(screen.getByRole('button', { name: '检查续训能力' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '开始训练' })).toBeEnabled())
  await userEvent.click(screen.getByRole('button', { name: '开始训练' }))
  expect(await screen.findByText(/Binding changed/)).toBeVisible()
  expect(screen.getByRole('button', { name: '开始训练' })).toBeDisabled()
})
