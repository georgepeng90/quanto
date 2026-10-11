import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'
import type { BacktestCatalogRow } from '../src/types'

const conditions = { period: { start: '2024-01-01', end: '2024-12-31' }, dataset_content_hash: 'data-1', price_mode: 'raw_cash', execution: { commission_rate: .0002 }, risk: { max_total_weight: .8 }, initial_cash: 100000, metrics: { annual_sessions: 252 }, valuation_frequency: 'daily' }
const row = (id: string, kind: BacktestCatalogRow['kind'], total_return: number | null, changes: Partial<BacktestCatalogRow> = {}): BacktestCatalogRow => ({
  id, job_id: `job-${id}`, kind, name: `研究 ${id}`, method: kind === 'policy' ? 'PPO' : kind === 'model' ? 'GRU' : '固定策略',
  state: 'succeeded', created_at: '2024-10-01T00:00:00+00:00', finished_at: '2024-10-01T00:01:00+00:00', error: null,
  start: '2024-01-01', end: '2024-12-31', symbols: ['510300'],
  metrics: total_return === null ? null : { total_return, annual_return: total_return / 2, max_drawdown: .2, sharpe: 1 },
  conditions, comparison_key: 'same-conditions', detail_url: kind === 'fixed' ? `/runs/${id}` : kind === 'policy' ? `/investing?backtest=${id}` : `/learning?backtest=${id}`,
  group_id: kind === 'fixed' ? 'batch-1' : null, identity_status: 'proven', canonical_assets: ['SSE:etf:510300'], asset_types: ['etf'], ...changes,
})
const fixed = row('fixed-1', 'fixed', .1)
const policy = row('policy-1', 'policy', .3)
const model = row('model-1', 'model', null, { state: 'running', comparison_key: null })
const different = row('policy-2', 'policy', .9, { conditions: { ...conditions, execution: { commission_rate: .001 } }, comparison_key: 'other-conditions' })
const app = () => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/runs'] })} />)

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/experiments') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/backtests/catalog?limit=50') return Response.json({ items: [fixed, policy], next_cursor: 'opaque-page' })
    if (path === '/api/backtests/catalog?cursor=opaque-page&limit=50') return Response.json({ items: [model, different], next_cursor: null })
    throw new Error(`unexpected ${path}`)
  }))
})

test('loads every catalog page and links each backtest to its detail view', async () => {
  app()
  expect(await screen.findByRole('link', { name: /研究 model-1/ })).toHaveAttribute('href', '/learning?backtest=model-1')
  const scrollArea = screen.getByRole('region', { name: '全部回测表格' })
  expect(scrollArea).toHaveAttribute('tabindex', '0')
  expect(within(scrollArea).getByRole('table', { name: '全部回测' })).toBeVisible()
  expect(screen.getByRole('link', { name: /研究 fixed-1/ })).toHaveAttribute('href', '/runs/fixed-1')
  expect(screen.getByRole('link', { name: /研究 policy-1/ })).toHaveAttribute('href', '/investing?backtest=policy-1')
  expect(screen.getByText(/4 条回测/)).toBeVisible()
  expect(globalThis.fetch).toHaveBeenCalledWith('/api/backtests/catalog?cursor=opaque-page&limit=50', expect.anything())
})

test('filters across kinds and states, sorts metrics with missing values last', async () => {
  app()
  await screen.findByText(/4 条回测/)
  await userEvent.selectOptions(screen.getByLabelText('类型筛选'), 'policy')
  expect(screen.queryByRole('link', { name: /研究 fixed-1/ })).toBeNull()
  expect(screen.getByRole('link', { name: /研究 policy-2/ })).toBeVisible()
  await userEvent.selectOptions(screen.getByLabelText('类型筛选'), 'all')
  await userEvent.selectOptions(screen.getByLabelText('指标排序'), 'return')
  const links = within(screen.getByRole('table', { name: '全部回测' })).getAllByRole('link')
  expect(links.map(link => link.textContent)).toEqual(['研究 policy-2', '研究 policy-1', '研究 fixed-1', '研究 model-1'])
  await userEvent.selectOptions(screen.getByLabelText('状态筛选'), 'running')
  expect(screen.getByRole('link', { name: /研究 model-1/ })).toBeVisible()
  expect(screen.queryByRole('link', { name: /研究 policy-1/ })).toBeNull()
})

test('compares aligned metrics and identifies the changed execution assumption', async () => {
  app()
  await screen.findByText(/4 条回测/)
  await userEvent.click(screen.getByLabelText('比较 fixed-1'))
  await userEvent.click(screen.getByLabelText('比较 policy-2'))
  expect(screen.getByText(/条件不同.*不可直接排名/)).toBeVisible()
  const comparison = screen.getByRole('table', { name: '所选回测指标比较' })
  expect(within(comparison).getByRole('row', { name: /累计收益率/ })).toHaveTextContent('10.00%')
  expect(within(comparison).getByRole('row', { name: /累计收益率/ })).toHaveTextContent('90.00%')
  expect(screen.getByRole('row', { name: /execution/ })).toHaveTextContent('0.001')
  await userEvent.click(screen.getByLabelText('比较 policy-2'))
  await userEvent.click(screen.getByLabelText('比较 policy-1'))
  expect(screen.getByText('条件一致，可直接比较这些结果。')).toBeVisible()
})

test('a succeeded row with a damaged saved result never appears directly comparable', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/backtests/catalog?limit=50'
    ? Response.json({ items: [fixed, row('damaged', 'policy', null, { result_error: '结果文件缺失' })], next_cursor: null }) : prior(input, init)))
  app()
  await screen.findByRole('link', { name: /研究 damaged/ })
  await userEvent.click(screen.getByLabelText('比较 fixed-1'))
  await userEvent.click(screen.getByLabelText('比较 damaged'))
  expect(screen.getByText('条件不同或结果未完成，不可直接排名；先核对下方差异。')).toBeVisible()
  expect(screen.getByText('结果文件缺失')).toBeVisible()
})

test('large fixed baskets stay compact in a row while the full list remains available', async () => {
  const symbols = Array.from({ length: 314 }, (_, index) => `SSE:etf:${String(index).padStart(6, '0')}`)
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/backtests/catalog?limit=50'
    ? Response.json({ items: [row('fixed-with-a-long-id', 'fixed', .1, { symbols, canonical_assets: symbols })], next_cursor: null }) : prior(input, init)))
  app()
  const link = await screen.findByRole('link', { name: /研究 fixed-with-a-long-id/ })
  const tableRow = link.closest('tr')!
  expect(within(tableRow).getByText('314 只 · SSE:etf:000000、SSE:etf:000001、SSE:etf:000002…')).toHaveAttribute('title', symbols.join('、'))
  expect(tableRow).not.toHaveTextContent('SSE:etf:000313')
  expect(within(tableRow).getByRole('link', { name: '研究 fixed-with-a-long-id' })).toBeVisible()
})

test('refreshes the catalog from the first page after a new job is submitted elsewhere', async () => {
  let firstPageReads = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/backtests/catalog?limit=50') {
      firstPageReads++
      if (firstPageReads > 1) return Response.json({ items: [fixed, policy, row('new-1', 'model', .5)], next_cursor: null })
    }
    return prior(input, init)
  }))
  app()
  await screen.findByText(/4 条回测/)
  await userEvent.click(screen.getByRole('button', { name: '刷新回测' }))
  expect(await screen.findByRole('link', { name: /研究 new-1/ })).toBeVisible()
  await waitFor(() => expect(firstPageReads).toBe(2))
})

test('StrictMode remount loads after aborting the first catalog request', async () => {
  let reads = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/backtests/catalog?limit=50') {
      reads++
    }
    return prior(input, init)
  }))
  render(<StrictMode><RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/runs'] })} /></StrictMode>)
  expect(await screen.findByRole('link', { name: /研究 model-1/ })).toBeVisible()
  expect(reads).toBeGreaterThanOrEqual(1)
})

test('submission refresh overtakes an unfinished initial catalog read', async () => {
  let reads = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/strategies') return Response.json({ items: [{ id: 's1', manifest: { name: '趋势轮动', parameter_schema: { type: 'object', properties: {} } } }], next_cursor: null })
    if (path === '/api/datasets') return Response.json({ items: [{ id: 'd1', manifest: { source: 'ETF日线', data_start: '2024-01-01', data_end: '2024-12-31' } }], next_cursor: null })
    if (path === '/api/definitions') return Response.json({ items: [{ ref: { kind: 'trading', id: 'etf-trading', version: 'v1' } }] })
    if (path === '/api/baskets') return Response.json({ items: [{ basket_id: 'b1', name: 'ETF 篮子', current_revision: 1, created_at: '' }], next_cursor: null })
    if (path === '/api/baskets/b1') return Response.json({ basket_id: 'b1', name: 'ETF 篮子', current_revision: 1, created_at: '', current: { basket_id: 'b1', revision: 1, members: [{ exchange: 'SSE', instrument_type: 'etf', code: '510300' }], created_at: '', selection_note: '', content_hash: 'h' }, revisions: [{ revision: 1, content_hash: 'h', created_at: '' }] })
    if (path === '/api/baskets/b1/revisions/1') return Response.json({ basket_id: 'b1', revision: 1, members: [{ exchange: 'SSE', instrument_type: 'etf', code: '510300' }], created_at: '', selection_note: '', content_hash: 'h' })
    if (path === '/api/capabilities/check') return Response.json({ operation: 'fixed_backtest', status: 'ready', request_hash: 'h', dataset_version_id: 'd1', input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null })
    if (path === '/api/experiments' && init?.method === 'POST') return Response.json({ id: 'e1', revision: 1, payload: JSON.parse(String(init.body)).payload })
    if (path === '/api/backtests/catalog?limit=50') {
      reads++
      return reads === 1 ? new Promise<Response>(() => {}) : Response.json({ items: [row('submitted', 'fixed', null, { state: 'queued' })], next_cursor: null })
    }
    if (path === '/api/runs' && init?.method === 'POST') return Response.json({ run_id: 'submitted' })
    return prior(input, init)
  }))
  app()
  await userEvent.click(screen.getByRole('button', { name: '新建回测' }))
  await screen.findByRole('option', { name: /趋势轮动/ })
  await userEvent.selectOptions(screen.getByLabelText('策略'), 's1')
  await userEvent.selectOptions(screen.getByLabelText('标的组合'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易策略'), 'etf-trading:v1')
  fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '100000' } })
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  expect(await screen.findByRole('link', { name: /研究 submitted/ })).toBeVisible()
  expect(reads).toBe(2)
})

test('refresh after exactly fifty rows finds later appended backtests', async () => {
  const rows = Array.from({ length: 50 }, (_, index) => row(`old-${index}`, 'fixed', .1))
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/backtests/catalog?limit=50'
    ? Response.json({ items: rows, next_cursor: null }) : prior(input, init)))
  app()
  expect(await screen.findByRole('link', { name: /研究 old-49/ })).toBeVisible()
  rows.push(row('appended', 'policy', .2))
  await userEvent.click(screen.getByRole('button', { name: '刷新回测' }))
  expect(await screen.findByRole('link', { name: /研究 appended/ })).toBeVisible()
})

test('unmount aborts an unfinished later catalog page', async () => {
  let pendingSignal: AbortSignal | undefined
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/backtests/catalog?limit=50') return Response.json({ items: [fixed], next_cursor: 'later' })
    if (path === '/api/backtests/catalog?cursor=later&limit=50') { pendingSignal = init?.signal as AbortSignal; return new Promise<Response>(() => {}) }
    return prior(input, init)
  }))
  const view = app()
  await waitFor(() => expect(pendingSignal).toBeDefined())
  view.unmount()
  expect(pendingSignal?.aborted).toBe(true)
})
