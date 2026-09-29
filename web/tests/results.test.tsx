import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'

const provenance = { strategy_revision_id: 'strategy-1', dataset_version_id: 'dataset-1', effective_strategy_params: { window: 4 }, price_mode: 'raw_cash', start: '2024-01-03', end: '2024-01-04' }
const run = { run_id: 'r1', job_id: 'j1', experiment_id: 'e1', experiment_revision: 1, manifest_hash: 'hash', source: 'web', parent_run_id: null, group_id: 'group-1', state: 'succeeded', completed_through: '2024-01-04', error: null, created_at: '2024-01-01T00:00:00Z', started_at: '2024-01-01T00:01:00Z', finished_at: '2024-01-01T00:02:00Z', detail_url: '/runs/r1', provenance }
const result = { run_id: 'r1', state: 'succeeded', completed_through: '2024-01-04', metrics: { final_equity: 100000, total_return: 0, sharpe: null, fill_count: 0, round_trip_count: 0 }, metric_definitions: { sharpe: { description: '零方差时不可用' }, total_return: { description: '累计收益' } }, warnings: [], metadata: { dataset_version_id: 'dataset-1', strategy_revision_id: 'strategy-1', price_mode: 'raw_cash' }, detail_url: '/runs/r1' }
const page = (items: unknown[]) => Response.json({ items, next_cursor: null })
const app = (path: string) => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />)

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'secret', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/runs') return page([{ ...run, provenance: undefined }])
    if (path === '/api/experiments') return page([])
    if (path === '/api/runs/r1') return Response.json(run)
    if (path === '/api/runs/r1/results') return Response.json(result)
    if (path === '/api/runs/r1/logs') return Response.json({ items: [], next_cursor: null, state: 'succeeded', completed_through: '2024-01-04' })
    if (path.startsWith('/api/runs/r1/tables/curves')) return page([{ date: '2024-01-03', equity: 100000, return: 0, drawdown: 0, benchmark_equity: 100000, benchmark_return: 0 }, { date: '2024-01-04', equity: 100000, return: 0, drawdown: 0, benchmark_equity: 100100, benchmark_return: 0.001 }])
    if (path.startsWith('/api/runs/r1/tables/equity')) return page([{ date: '2024-01-04', cash: 100000, receivables: 0, market_value: 0, equity: 100000, stale_value: 0 }])
    if (path.startsWith('/api/runs/r1/tables/orders')) return page([{ id: 'o1', signal_date: '2024-01-03', execution_date: '2024-01-04', symbol: '000001', side: 'buy', status: 'filled', reason: '<script>alert(1)</script>', source: 'strategy', quantity: 100, filled_quantity: 100, expired_quantity: 0 }])
    if (path.startsWith('/api/runs/r1/tables/fills')) return page([{ order_id: 'o1', trade_date: '2024-01-04', symbol: '000001', side: 'buy', quantity: 100, price: 1, commission: 5, slippage_cost: 1 }])
    if (path.startsWith('/api/runs/r1/tables/signals')) return page([{ date: '2024-01-03', intent: 'target', weights: { '000001': 1 }, reasons: { '000001': '目标' }, diagnostics: {} }])
    if (path.startsWith('/api/runs/r1/tables/')) return page([])
    if (path.startsWith('/api/runs/r1/exports/')) return new Response(new Uint8Array([1, 2, 3]), { status: 200, headers: { 'Content-Type': 'application/zip' } })
    throw new Error(`unexpected ${init?.method || 'GET'} ${path}`)
  }))
})

test('renders cash-only results and undefined ratios from the backend', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/tables/orders') || String(input).includes('/tables/fills') || String(input).includes('/tables/signals')) return page([])
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText(/最终权益/)).toBeVisible()
  expect(screen.getByText('不可用')).toBeVisible()
  expect(screen.getAllByText(/零方差时不可用/).length).toBeGreaterThan(0)
  expect(screen.getByText(/原始现金/)).toBeVisible()
  expect(screen.getAllByText(/100000/).length).toBeGreaterThan(0)
  expect(screen.getByText(/该筛选下无成交/)).toBeVisible()
})

test('links fills to their order and dated signal without treating risk as a direct join', async () => {
  app('/runs/r1')
  const fill = await screen.findByText(/佣金 5/)
  expect(fill.closest('article')?.querySelector('a[href="#order-o1"]')).toBeTruthy()
  expect(screen.getByText(/目标权重/)).toBeVisible()
  expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeVisible()
  expect(document.querySelector('script:not([src])')).toBeNull()
})

test('downloads authenticated ZIP and HTML report bytes without token in URL', async () => {
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const createObjectURL = vi.fn(() => 'blob:report')
  const revokeObjectURL = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL })
  app('/runs/r1')
  await userEvent.click(await screen.findByRole('button', { name: /CSV.*ZIP/ }))
  await waitFor(() => expect(createObjectURL).toHaveBeenCalled())
  const calls = vi.mocked(fetch).mock.calls.filter(([url]) => String(url).includes('/exports/'))
  expect(calls[0][0]).toBe('/api/runs/r1/exports/csv')
  expect((calls[0][1]?.headers as Record<string, string>).Authorization).toBe('Bearer secret')
  expect(revokeObjectURL).toHaveBeenCalled()
  expect(click).toHaveBeenCalled()
  click.mockRestore()
})

test('run list shows frozen strategy, params, dataset and final metrics with state and group filters', async () => {
  const calls: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => { calls.push(String(input)); return prior(input, init) }))
  app('/runs')
  expect(await screen.findByText(/strategy-1/)).toBeVisible()
  expect(screen.getByText(/dataset-1/)).toBeVisible()
  expect(screen.getByText(/window.*4/)).toBeVisible()
  expect(screen.getByText(/最终权益.*100000/)).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: '刷新' }))
  await waitFor(() => expect(calls.filter(path => path === '/api/runs').length).toBeGreaterThan(1))
  expect(calls.filter(path => path === '/api/runs/r1/results')).toHaveLength(1)
  await userEvent.selectOptions(screen.getByLabelText('批次筛选'), 'group-1')
  expect(screen.getByRole('link', { name: 'r1' })).toBeVisible()
  await userEvent.selectOptions(screen.getByLabelText('状态筛选'), 'failed')
  expect(screen.getByText('暂无匹配运行。')).toBeVisible()
})
