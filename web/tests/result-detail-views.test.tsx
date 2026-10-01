import { render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'
import { ResultTables } from '../src/components/ResultTables'
import { RunTrace } from '../src/components/RunTrace'

const tableCalls: { url: URL; signal?: AbortSignal }[] = []
let tableReply: (name: string, url: URL) => Promise<Response>

const page = (items: unknown[], next_cursor: string | null = null, total = items.length) =>
  Response.json({ items, next_cursor, total })

beforeEach(() => {
  tableCalls.length = 0
  tableReply = async () => page([])
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/session') return Response.json({ token: 'secret', api_base: '/api', timezone: 'Asia/Shanghai' })
    const url = new URL(String(input), 'http://localhost')
    if (url.pathname.startsWith('/api/runs/r1/tables/')) {
      tableCalls.push({ url, signal: init?.signal as AbortSignal | undefined })
      return tableReply(url.pathname.split('/').at(-1) || '', url)
    }
    throw new Error(`Unexpected ${url}`)
  }))
})

test('initial detail reads one latest-day account page and never follows its cursor', async () => {
  tableReply = async name => name === 'equity' ? page([{ date: '2024-01-04', equity: 10 }], 'next', 201) : page([])
  render(<ResultTables runId="r1" defaultDate="2024-01-04" />)
  expect(await screen.findByRole('cell', { name: '10' })).toBeVisible()
  expect(tableCalls).toHaveLength(1)
  expect(tableCalls[0].url.pathname.endsWith('/equity')).toBe(true)
  expect(tableCalls[0].url.searchParams.get('date_from')).toBe('2024-01-04')
  expect(tableCalls[0].url.searchParams.get('date_to')).toBe('2024-01-04')
  expect(tableCalls[0].url.searchParams.get('limit')).toBe('100')
  expect(screen.getByText(/1–1.*201/)).toBeVisible()
})

test('next page fetches exactly one cursor page and switch fetches only the selected table', async () => {
  tableReply = async (name, url) => name === 'equity'
    ? url.searchParams.get('cursor') === 'second' ? page([{ date: '2024-01-04', equity: 20 }], null, 101) : page([{ date: '2024-01-04', equity: 10 }], 'second', 101)
    : page([{ date: '2024-01-04', symbol: '000001' }], null, 1)
  render(<ResultTables runId="r1" defaultDate="2024-01-04" />)
  await screen.findByRole('cell', { name: '10' })
  await userEvent.click(screen.getByRole('button', { name: '下一页' }))
  expect(await screen.findByRole('cell', { name: '20' })).toBeVisible()
  expect(tableCalls).toHaveLength(2)
  expect(tableCalls[1].url.searchParams.get('cursor')).toBe('second')
  await userEvent.click(screen.getByRole('tab', { name: '持仓' }))
  expect(await screen.findByRole('cell', { name: '000001' })).toBeVisible()
  expect(tableCalls.map(call => call.url.pathname.split('/').at(-1))).toEqual(['equity', 'equity', 'positions'])
})

test('range and symbol submission queries the server and resets the page', async () => {
  tableReply = async (_name, url) => url.searchParams.get('date_to') === '2024-01-03' ? page([{ date: '2024-01-02', equity: 30 }]) : page([{ date: '2024-01-04', equity: 10 }], 'second', 101)
  render(<ResultTables runId="r1" defaultDate="2024-01-04" startDate="2024-01-01" endDate="2024-01-04" />)
  await screen.findByRole('cell', { name: '10' })
  await userEvent.click(screen.getByRole('button', { name: '下一页' }))
  await waitFor(() => expect(tableCalls).toHaveLength(2))
  await userEvent.clear(screen.getByLabelText('开始日期'))
  await userEvent.type(screen.getByLabelText('开始日期'), '2024-01-01')
  await userEvent.clear(screen.getByLabelText('结束日期'))
  await userEvent.type(screen.getByLabelText('结束日期'), '2024-01-03')
  await userEvent.type(screen.getByLabelText('标的代码'), '000001')
  await userEvent.click(screen.getByRole('button', { name: '查询明细' }))
  expect(await screen.findByRole('cell', { name: '30' })).toBeVisible()
  const last = tableCalls.at(-1)!.url.searchParams
  expect(last.get('date_from')).toBe('2024-01-01')
  expect(last.get('date_to')).toBe('2024-01-03')
  expect(last.has('symbol')).toBe(false)
  expect(last.has('cursor')).toBe(false)
})

test('symbol filter applies to holdings but leaves global account rows visible', async () => {
  tableReply = async name => name === 'equity' ? page([{ date: '2024-01-04', equity: 12 }]) : page([{ date: '2024-01-04', symbol: '000001' }])
  render(<ResultTables runId="r1" defaultDate="2024-01-04" />)
  await screen.findByRole('cell', { name: '12' })
  await userEvent.type(screen.getByLabelText('标的代码'), '000001')
  await userEvent.click(screen.getByRole('button', { name: '查询明细' }))
  expect(await screen.findByRole('cell', { name: '12' })).toBeVisible()
  expect(tableCalls.at(-1)!.url.searchParams.has('symbol')).toBe(false)
  expect(screen.getByText(/账户.*不按标的筛选/)).toBeVisible()
  await userEvent.click(screen.getByRole('tab', { name: '持仓' }))
  expect(await screen.findByRole('cell', { name: '000001' })).toBeVisible()
  expect(tableCalls.at(-1)!.url.searchParams.get('symbol')).toBe('000001')
})

test('recent 30 days spans thirty dates across the March boundary', async () => {
  tableReply = async () => page([])
  render(<ResultTables runId="r1" defaultDate="2024-03-31" />)
  await screen.findByText('无账户记录。')
  await userEvent.click(screen.getByRole('button', { name: '最近30天' }))
  await waitFor(() => expect(tableCalls).toHaveLength(2))
  expect(tableCalls[1].url.searchParams.get('date_from')).toBe('2024-03-02')
  expect(tableCalls[1].url.searchParams.get('date_to')).toBe('2024-03-31')
})

test('switching tabs aborts stale reads and stale responses cannot replace new data', async () => {
  let resolveOld!: (response: Response) => void
  tableReply = async name => name === 'equity' ? new Promise<Response>(resolve => { resolveOld = resolve }) : page([{ date: '2024-01-04', symbol: 'new' }])
  render(<ResultTables runId="r1" defaultDate="2024-01-04" />)
  await waitFor(() => expect(tableCalls).toHaveLength(1))
  await userEvent.click(screen.getByRole('tab', { name: '持仓' }))
  expect(await screen.findByRole('cell', { name: 'new' })).toBeVisible()
  expect(tableCalls[0].signal?.aborted).toBe(true)
  resolveOld(page([{ date: '2024-01-04', equity: 999 }]))
  await waitFor(() => expect(screen.queryByRole('cell', { name: '999' })).toBeNull())
})

test('submitting a new symbol aborts the previous filter while preserving leading zeros', async () => {
  let resolveOld!: (response: Response) => void
  tableReply = async (name, url) => name === 'equity' ? page([]) : url.searchParams.has('symbol') ? page([{ date: '2024-01-04', symbol: '000001' }]) : new Promise<Response>(resolve => { resolveOld = resolve })
  render(<ResultTables runId="r1" defaultDate="2024-01-04" />)
  await screen.findByText('无账户记录。')
  await userEvent.click(screen.getByRole('tab', { name: '持仓' }))
  await waitFor(() => expect(tableCalls).toHaveLength(2))
  await userEvent.type(screen.getByLabelText('标的代码'), '000001')
  await userEvent.click(screen.getByRole('button', { name: '查询明细' }))
  expect(await screen.findByRole('cell', { name: '000001' })).toBeVisible()
  expect(tableCalls[1].signal?.aborted).toBe(true)
  expect(tableCalls[2].url.searchParams.get('symbol')).toBe('000001')
  resolveOld(page([{ date: '2024-01-04', symbol: 'stale' }]))
  await waitFor(() => expect(screen.queryByRole('cell', { name: 'stale' })).toBeNull())
})

test('strict mode starts only one effective account request', async () => {
  tableReply = async () => page([{ date: '2024-01-04', equity: 10 }])
  render(<StrictMode><ResultTables runId="r1" defaultDate="2024-01-04" /></StrictMode>)
  expect(await screen.findByRole('cell', { name: '10' })).toBeVisible()
  expect(tableCalls).toHaveLength(1)
})

test('latest day follows run progress until the user submits a custom range', async () => {
  tableReply = async (_name, url) => page([{ date: url.searchParams.get('date_from'), equity: 10 }])
  const view = render(<ResultTables runId="r1" defaultDate="2024-01-03" />)
  expect(await screen.findByRole('cell', { name: '2024-01-03' })).toBeVisible()
  view.rerender(<ResultTables runId="r1" defaultDate="2024-01-04" />)
  expect(await screen.findByRole('cell', { name: '2024-01-04' })).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: '查询明细' }))
  view.rerender(<ResultTables runId="r1" defaultDate="2024-01-05" />)
  expect(screen.getByRole('cell', { name: '2024-01-04' })).toBeVisible()
  expect(tableCalls.at(-1)!.url.searchParams.get('date_from')).toBe('2024-01-04')
})

test('refresh token re-queries only the current detail page', async () => {
  tableReply = async (_name, url) => url.searchParams.has('cursor') ? page([{ date: '2024-01-04', equity: 20 }], null, 101) : page([{ date: '2024-01-04', equity: 10 }], 'second', 101)
  const view = render(<ResultTables runId="r1" defaultDate="2024-01-04" refreshToken={0} />)
  await screen.findByRole('cell', { name: '10' })
  await userEvent.click(screen.getByRole('button', { name: '下一页' }))
  await screen.findByRole('cell', { name: '20' })
  view.rerender(<ResultTables runId="r1" defaultDate="2024-01-04" refreshToken={1} />)
  await waitFor(() => expect(tableCalls).toHaveLength(3))
  expect(tableCalls[2].url.searchParams.get('cursor')).toBe('second')
})

test('terminal transition re-queries a partial detail page even when the date is unchanged', async () => {
  let final = false
  tableReply = async name => name === 'matched_trades' && final
    ? page([{ symbol: '000001', entry_date: '2024-01-02', exit_date: '2024-01-04', quantity: 100, cost_basis: 10, proceeds: 12, realized_pnl: 2, holding_sessions: 2 }])
    : page([])
  const view = render(<ResultTables runId="r1" defaultDate="2024-01-04" live />)
  await screen.findByText('无账户记录。')
  await userEvent.click(screen.getByRole('tab', { name: '已平仓 FIFO 片段' }))
  await screen.findByText('无已平仓 FIFO 片段记录。')
  expect(tableCalls.filter(call => call.url.pathname.endsWith('/matched_trades'))).toHaveLength(1)
  final = true
  view.rerender(<ResultTables runId="r1" defaultDate="2024-01-04" live={false} />)
  expect(await screen.findByRole('cell', { name: '000001' })).toBeVisible()
  expect(tableCalls.filter(call => call.url.pathname.endsWith('/matched_trades'))).toHaveLength(2)
})

test('a detail failure remains an error until manual retry succeeds', async () => {
  let failed = true
  tableReply = async () => failed ? Response.json({ code: 'INVALID_ARGUMENT', message: '产物校验失败' }, { status: 422 }) : page([{ date: '2024-01-04', equity: 42 }])
  render(<ResultTables runId="r1" defaultDate="2024-01-04" />)
  expect(await screen.findByRole('alert')).toHaveTextContent('产物校验失败')
  expect(screen.queryByText('无账户记录。')).toBeNull()
  expect(tableCalls).toHaveLength(1)
  failed = false
  await userEvent.click(screen.getByRole('button', { name: '重试账户' }))
  expect(await screen.findByRole('cell', { name: '42' })).toBeVisible()
  expect(tableCalls).toHaveLength(2)
})

test('trace queries four bounded context pages and paginates each section separately', async () => {
  tableReply = async (name, url) => name === 'orders'
    ? url.searchParams.get('cursor') === 'more' ? page([{ id: 'o2', signal_date: '2024-01-03', symbol: '000001' }], null, 101) : page([{ id: 'o1', signal_date: '2024-01-03', symbol: '000001' }], 'more', 101)
    : page([])
  render(<RunTrace runId="r1" defaultDate="2024-01-04" />)
  expect(await screen.findByText(/订单 o1/)).toBeVisible()
  expect(tableCalls).toHaveLength(4)
  expect(tableCalls.map(call => call.url.pathname.split('/').at(-1)).sort()).toEqual(['fills', 'orders', 'risk_events', 'signals'])
  for (const call of tableCalls) {
    expect(call.url.searchParams.get('context_date')).toBe('2024-01-04')
    expect(call.url.searchParams.get('limit')).toBe('100')
  }
  const orders = screen.getByRole('region', { name: '订单追踪' })
  await userEvent.click(within(orders).getByRole('button', { name: '下一页' }))
  expect(await within(orders).findByText(/订单 o2/)).toBeVisible()
  expect(tableCalls).toHaveLength(5)
  expect(tableCalls[4].url.searchParams.get('cursor')).toBe('more')
  expect(within(orders).queryByText(/订单 o1/)).toBeNull()
})

test('trace sends symbol with context date and never creates a link to an off-page signal', async () => {
  tableReply = async name => name === 'orders' ? page([{ id: 'o1', signal_date: '2024-01-03', execution_date: '2024-01-04', symbol: '000001' }]) : page([])
  render(<RunTrace runId="r1" defaultDate="2024-01-04" />)
  await screen.findByText(/订单 o1/)
  expect(document.querySelector('a[href="#signal-2024-01-03"]')).toBeNull()
  expect(screen.getByText(/当前信号页未显示/)).toBeVisible()
  await userEvent.type(screen.getByLabelText('标的'), '000001')
  await userEvent.click(screen.getByRole('button', { name: '查询追踪' }))
  await waitFor(() => expect(tableCalls).toHaveLength(8))
  expect(tableCalls.slice(4).every(call => call.url.searchParams.get('symbol') === '000001' && call.url.searchParams.get('context_date') === '2024-01-04')).toBe(true)
})

test('trace requires a bounded date before querying a new context', async () => {
  tableReply = async () => page([])
  render(<RunTrace runId="r1" defaultDate="2024-01-04" startDate="2024-01-01" endDate="2024-01-05" />)
  await waitFor(() => expect(tableCalls).toHaveLength(4))
  const input = screen.getByLabelText('模拟日期') as HTMLInputElement
  expect(input).toBeRequired()
  expect(input).toHaveAttribute('min', '2024-01-01')
  expect(input).toHaveAttribute('max', '2024-01-05')
  await userEvent.clear(input)
  await userEvent.click(screen.getByRole('button', { name: '查询追踪' }))
  expect(tableCalls).toHaveLength(4)
})

test('trace day shortcuts query from the draft date, keep the symbol and reset pagination', async () => {
  tableReply = async (name, url) => name === 'orders' ? page([{
    id: url.searchParams.has('cursor') ? 'o2' : 'o1', signal_date: url.searchParams.get('context_date'), symbol: '000001'
  }], 'more', 101) : page([])
  render(<RunTrace runId="r1" defaultDate="2024-03-02" startDate="2024-02-28" endDate="2024-03-03" />)
  await screen.findByText(/订单 o1/)
  await userEvent.click(within(screen.getByRole('region', { name: '订单追踪' })).getByRole('button', { name: '下一页' }))
  await screen.findByText(/订单 o2/)
  await userEvent.clear(screen.getByLabelText('模拟日期'))
  await userEvent.type(screen.getByLabelText('模拟日期'), '2024-03-01')
  await userEvent.type(screen.getByLabelText('标的'), ' 000001 ')
  await userEvent.click(screen.getByRole('button', { name: '前一天' }))
  await waitFor(() => expect(tableCalls).toHaveLength(9))
  expect(screen.getByLabelText('模拟日期')).toHaveValue('2024-02-29')
  expect(tableCalls.slice(5).every(call => call.url.searchParams.get('context_date') === '2024-02-29' && call.url.searchParams.get('symbol') === '000001' && !call.url.searchParams.has('cursor'))).toBe(true)
  expect(tableCalls[4].signal?.aborted).toBe(true)
  expect(await screen.findByText(/订单 o1/)).toBeVisible()
  expect(within(screen.getByRole('region', { name: '订单追踪' })).getByRole('button', { name: '上一页' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '后一天' }))
  await waitFor(() => expect(tableCalls).toHaveLength(13))
  expect(screen.getByLabelText('模拟日期')).toHaveValue('2024-03-01')
  expect(tableCalls.slice(9).every(call => call.url.searchParams.get('context_date') === '2024-03-01' && call.url.searchParams.get('symbol') === '000001')).toBe(true)
})

test('trace day shortcuts cross the year boundary and disable empty or out-of-range navigation', async () => {
  render(<RunTrace runId="r1" defaultDate="2023-12-31" startDate="2023-12-31" endDate="2024-01-01" />)
  await waitFor(() => expect(tableCalls).toHaveLength(4))
  expect(screen.getByRole('button', { name: '前一天' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '后一天' }))
  await waitFor(() => expect(tableCalls).toHaveLength(8))
  expect(screen.getByLabelText('模拟日期')).toHaveValue('2024-01-01')
  expect(screen.getByRole('button', { name: '后一天' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '前一天' })).toBeEnabled()
  await userEvent.clear(screen.getByLabelText('模拟日期'))
  expect(screen.getByRole('button', { name: '前一天' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '后一天' })).toBeDisabled()
  expect(tableCalls).toHaveLength(8)
})

test('terminal transition refreshes trace context on the same day', async () => {
  let final = false
  tableReply = async name => name === 'signals' && final
    ? page([{ date: '2024-01-04', intent: 'target', weights: { '000001': 1 }, reasons: {}, diagnostics: {}, approved_intent: 'target', approved_weights: { '000001': 1 }, approved_reasons: {} }])
    : page([])
  const view = render(<RunTrace runId="r1" defaultDate="2024-01-04" live />)
  await screen.findByText('该筛选下无策略信号。')
  expect(tableCalls).toHaveLength(4)
  final = true
  view.rerender(<RunTrace runId="r1" defaultDate="2024-01-04" live={false} />)
  expect(await screen.findByText(/策略原始目标权重/)).toBeVisible()
  expect(tableCalls.filter(call => call.url.pathname.endsWith('/signals'))).toHaveLength(2)
})
