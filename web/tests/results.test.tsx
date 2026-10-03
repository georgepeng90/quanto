import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'

const provenance = { strategy_revision_id: 'strategy-1', dataset_version_id: 'dataset-1', effective_strategy_params: { window: 4 }, price_mode: 'raw_cash', start: '2024-01-03', end: '2024-01-04', symbols: ['000001', '000002', '000003', '000004', '000005', '000006'] }
const run = { run_id: 'r1', job_id: 'j1', experiment_id: 'e1', experiment_revision: 1, manifest_hash: 'hash', source: 'web', parent_run_id: null, group_id: 'group-1', state: 'succeeded', completed_through: '2024-01-04', error: null, created_at: '2024-01-01T00:00:00Z', started_at: '2024-01-01T00:01:00Z', finished_at: '2024-01-01T00:02:00Z', detail_url: '/runs/r1', provenance }
const result = { run_id: 'r1', state: 'succeeded', completed_through: '2024-01-04', metrics: { final_equity: 100000, total_return: 0, sharpe: null, fill_count: 0, round_trip_count: 0 }, metric_definitions: { sharpe: { description: '零方差时不可用' }, total_return: { description: '累计收益' } }, warnings: [], metadata: { dataset_version_id: 'dataset-1', strategy_revision_id: 'strategy-1', price_mode: 'raw_cash' }, held_symbols: ['000001', '000002'], detail_url: '/runs/r1' }
const page = (items: unknown[]) => Response.json({ items, next_cursor: null, total: items.length })
const app = (path: string) => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />)

beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} })
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'secret', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/runs') return page([{ ...run, provenance: undefined }])
    if (path === '/api/experiments') return page([])
    if (path === '/api/runs/r1') return Response.json(run)
    if (path === '/api/runs/r1/results') return Response.json(result)
    if (path.startsWith('/api/runs/r1/logs')) return Response.json({ items: [], next_cursor: null, state: 'succeeded', completed_through: '2024-01-04' })
    if (path.startsWith('/api/runs/r1/charts/curves')) return Response.json({ items: [{ date: '2024-01-03', equity: 100000, return: 0, drawdown: 0, benchmark_equity: 100000, benchmark_return: 0 }, { date: '2024-01-04', equity: 100000, return: 0, drawdown: 0, benchmark_equity: 100100, benchmark_return: 0.001 }], total_rows: 2, sampled: false })
    if (path.startsWith('/api/runs/r1/charts/')) return Response.json({ items: [], total_rows: 0, sampled: false })
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
  expect((await screen.findAllByText(/100000/)).length).toBeGreaterThan(0)
  expect(await screen.findByText(/该筛选下无成交/)).toBeVisible()
})

test('links fills to their order and dated signal without treating risk as a direct join', async () => {
  app('/runs/r1')
  const fill = await screen.findByText(/佣金 5/)
  expect(fill.closest('article')?.querySelector('a[href="#order-o1"]')).toBeTruthy()
  expect(screen.getByText(/目标权重/)).toBeVisible()
  expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeVisible()
  expect(document.querySelector('script:not([src])')).toBeNull()
})

test('final performance distinguishes actual held symbols from configured backtest scope', async () => {
  app('/runs/r1')
  expect(await screen.findByText('实际持有 2 只')).toBeVisible()
  expect(screen.getByText('回测期间持有标的')).toBeVisible()
  await userEvent.click(screen.getByText('回测期间持有标的'))
  expect(screen.getByText('000001、000002')).toBeVisible()
  expect(screen.getByText('回测范围 6 只')).toBeVisible()
})

test('an empty completed held-symbol list reports zero and no held instruments', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/runs/r1/results') return Response.json({ ...result, held_symbols: [] })
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText('实际持有 0 只')).toBeVisible()
  await userEvent.click(screen.getByText('回测期间持有标的'))
  expect(screen.getByText('回测期间未持有标的')).toBeVisible()
})

test('loads independent summaries and requests positions only after selecting its tab', async () => {
  let release!: () => void
  const delayed = new Promise<void>(resolve => { release = resolve })
  let positionRequests = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path.includes('/tables/positions')) {
      positionRequests++
      await delayed
      return Response.json({ items: [{ date: '2024-01-04', symbol: '000001', shares: 100 }], next_cursor: '100', total: 40000 })
    }
    return prior(input, init)
  }))
  app('/runs/r1')
  try {
    expect(await screen.findByRole('img', { name: /equity图表/ })).toBeVisible()
    expect((await screen.findAllByRole('cell', { name: '100000' })).length).toBeGreaterThan(0)
    expect(await screen.findByText(/佣金 5/)).toBeVisible()
    expect(positionRequests).toBe(0)
    await userEvent.click(screen.getByRole('tab', { name: '持仓' }))
    expect(await screen.findByText('正在加载持仓…')).toBeVisible()
    expect(screen.queryByText('无持仓记录。')).toBeNull()
    expect(screen.getByRole('img', { name: /equity图表/ })).toBeVisible()
  } finally { release() }
  expect(await screen.findByRole('cell', { name: '000001' })).toBeVisible()
  expect(positionRequests).toBe(1)
})

test('retries a transient summary failure without reloading successful details', async () => {
  let curveRequests = 0
  let equityRequests = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path.includes('/tables/equity')) equityRequests++
    if (path.includes('/charts/curves') && ++curveRequests === 1) return Response.json({ code: 'HTTP_ERROR', message: '服务暂时不可用' }, { status: 503 })
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText('正在加载权益与基准…')).toBeVisible()
  expect(screen.queryByText('暂无权益与基准数据。')).toBeNull()
  expect(await screen.findByText(/佣金 5/)).toBeVisible()
  expect(await screen.findByRole('img', { name: /equity图表/ }, { timeout: 4000 })).toBeVisible()
  expect(curveRequests).toBe(2)
  expect(equityRequests).toBe(1)
})

test('shows an invalid table as a load failure and allows a manual retry', async () => {
  let recovered = false
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/tables/orders') && !recovered) return Response.json({ code: 'INVALID_ARGUMENT', message: '产物校验失败' }, { status: 422 })
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText(/订单加载失败.*产物校验失败/)).toBeVisible()
  expect(screen.queryByText('该筛选下无订单。')).toBeNull()
  expect(await screen.findByRole('img', { name: /equity图表/ })).toBeVisible()
  recovered = true
  await userEvent.click(screen.getByRole('button', { name: '重试订单' }))
  expect(await screen.findByText(/订单 o1/)).toBeVisible()
})

test('order enums have Chinese labels while raw codes remain available', async () => {
  app('/runs/r1')
  await userEvent.click(await screen.findByRole('tab', { name: '订单' }))
  expect(await screen.findByRole('cell', { name: '买入' })).toHaveAttribute('title', 'buy')
  expect(screen.getByRole('cell', { name: '已成交' })).toHaveAttribute('title', 'filled')
  expect(screen.getByRole('cell', { name: '策略' })).toHaveAttribute('title', 'strategy')
  expect(screen.getByRole('cell', { name: '000001' })).toBeVisible()
})

test('execution-day filter keeps the visible order signal-day context reachable', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/tables/risk_events')) return page([{ date: '2024-01-03', code: 'RISK_NOTE', message: '当日风控说明', details: {} }])
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByLabelText('模拟日期')).toHaveValue('2024-01-04')
  const order = (await screen.findByText(/订单 o1/)).closest('article')
  expect(order?.querySelector('a[href="#signal-2024-01-03"]')).toBeTruthy()
  expect(document.getElementById('signal-2024-01-03')).toBeVisible()
  expect(screen.getByText('当日风控说明')).toBeVisible()
  expect(screen.getAllByText(/信号日上下文/).length).toBeGreaterThan(0)
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

test('demo disclosure remains visible beside performance in the fixed detail view', async () => {
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/runs/r1') return Response.json({ ...run, provenance: { ...provenance, is_demo: true, source: 'synthetic fixture', currency: 'CNY' } })
    return previous(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText(/演示数据：合成绩效/)).toBeVisible()
  expect(screen.getByText(/演示数据：合成绩效/)).toHaveTextContent('synthetic fixture')
})

test('shows separate raw signal and approved target, and marks historical signal uncertainty', async () => {
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('/tables/signals')) return page([
      { date: '2024-01-03', intent: 'target', weights: { A: 1 }, reasons: { A: 'original' }, diagnostics: { score: 42 }, approved_intent: 'target', approved_weights: { A: .5 }, approved_reasons: { A: 'risk_limit' }, approved_diagnostics: {} },
      { date: '2024-01-04', intent: 'target', weights: { A: .5 }, reasons: {}, diagnostics: {} },
    ])
    return previous(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText(/策略原始目标权重/)).toHaveTextContent('"A":1')
  expect(screen.getByText(/风控批准目标权重/)).toHaveTextContent('"A":0.5')
  expect(screen.getByText(/历史记录未保存原始信号/)).toBeVisible()
})
