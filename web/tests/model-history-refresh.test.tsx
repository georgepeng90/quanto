import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, expect, test, vi } from 'vitest'
import { ModelDetailPage } from '../src/pages/ModelDetailPage'

afterEach(() => vi.useRealTimers())

function setup() {
  let count = 305
  const historyCalls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://local')
    if (url.pathname === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    if (url.pathname === '/api/capabilities/check') return Response.json({ operation: 'resume', status: 'ready',
      request_hash: 'resume-hash', dataset_version_id: 'd1', input_hashes: {}, checks: [], assumptions: [],
      limitations: [], checker_version: 'v1', generated_at: null })
    if (url.pathname === '/api/investing/models/m1') return Response.json({
      id: 'm1', name: 'Model', dataset_id: 'd1', state: 'ready', active_job_id: null, active_session: null,
      total_timesteps: 156160, version_count: count, session_count: count, latest_version_id: 'latest-job', capabilities: { cuda_available: true },
      framework_binding: { model_id: 'm1', origin: 'new', binding_hash: 'binding-hash', original_spec_hash: 'spec-hash',
        basket_revision_ref: { basket_id: 'basket-1', revision: 1 },
        ordered_pairs: [{ instrument_id: { exchange: 'SZSE', instrument_type: 'etf', code: '000001' }, local_symbol: 'ETF1', evidence_ref: 'fixture' }],
        definitions: [], evidence: {} }, framework_binding_check: null,
      spec: { backtest: { symbols: ['ETF1'] }, total_timesteps: 512, device: 'cuda', frequency: 'weekly' },
    })
    if (url.pathname.endsWith('/history')) {
      historyCalls.push(url.search)
      if (url.searchParams.get('kind') === 'versions') return Response.json({ items: [{ id: 'latest-job', round_number: count, best_round_number: count, selection_valid: true, validation_best: {} }], total: count, page: 1, page_size: 1, total_pages: count, anchor: 610 })
      return Response.json({ items: [], total: 305, page: 1, page_size: 10, total_pages: 31, anchor: 610 })
    }
    throw new Error(`Unexpected request ${url.pathname}`)
  }))
  const view = render(<RouterProvider router={createMemoryRouter([{ path: '/models/:id', element: <ModelDetailPage /> }], { initialEntries: ['/models/m1'] })} />)
  return { historyCalls, view, add: () => { count += 1 } }
}

test('initial loading fetches one history page and polling only refreshes the summary', async () => {
  vi.useFakeTimers()
  let state!: ReturnType<typeof setup>
  await act(async () => { state = setup() })
  expect(state.historyCalls).toHaveLength(2)
  expect(state.historyCalls.filter(query => query.includes('kind=sessions'))).toHaveLength(1)
  expect(state.historyCalls.filter(query => query.includes('kind=versions'))).toHaveLength(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(state.historyCalls).toHaveLength(2)
  state.view.unmount()
})

test('successive new rounds are counted against frozen pages, not the preceding poll', async () => {
  vi.useFakeTimers()
  let state!: ReturnType<typeof setup>
  await act(async () => { state = setup() })
  state.add()
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(screen.getAllByText(/有1条新记录/)).toHaveLength(1)
  state.add()
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(screen.getAllByText(/有2条新记录/)).toHaveLength(1)
  state.view.unmount()
})

test('a submitted training job can still be cancelled when summary refresh fails', async () => {
  const state = setup()
  await screen.findByRole('heading', { name: 'Model' })
  const prior = globalThis.fetch
  let submitted = false, cancelled = false
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://local').pathname
    if (path.endsWith('/train')) { submitted = true; return Response.json({ id: 'new-job', state: 'queued' }) }
    if (path.endsWith('/new-job/cancel')) { cancelled = true; return Response.json({ id: 'new-job', state: 'cancel_requested' }) }
    if (submitted && path === '/api/investing/models/m1') return Response.json({ message: 'Summary temporarily unavailable' }, { status: 503 })
    return prior(input, init)
  }))
  fireEvent.click(screen.getByRole('button', { name: '检查续训能力' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '继续训练' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '继续训练' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: '继续训练' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '取消当前训练' }))
  await waitFor(() => expect(cancelled).toBe(true))
  state.view.unmount()
})
