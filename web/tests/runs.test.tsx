import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'
import { RunProgress } from '../src/components/RunProgress'
import type { RunRecord } from '../src/types'

const experiment = { id: 'e1', revision: 3, payload: { strategy_revision_id: 's1', dataset_version_id: 'd1', strategy_params: { window: 4 }, backtest_spec: { start: '2024-01-01', end: '2024-01-31' }, sample_purpose: 'exploration' } }
const queued = { run_id: 'r1', job_id: 'j1', experiment_id: 'e1', experiment_revision: 3, manifest_hash: 'hash', source: 'web', parent_run_id: null, group_id: null, state: 'queued', completed_through: null, error: null, created_at: '2024-01-01T00:00:00+00:00', started_at: null, finished_at: null, detail_url: '/runs/r1' }
const running = { ...queued, state: 'running', completed_through: '2024-01-03', started_at: '2024-01-01T00:01:00+00:00' }
const event = { seq: 1, phase: '回测执行', asof: '2024-01-03', completed_days: 3, total_days: 10, level: 'info', code: 'DAY_DONE', message: '已完成交易日', payload: {} }
const empty = { items: [], next_cursor: null, total: 0 }
const requestUrl = (input: RequestInfo | URL) => new URL(String(input), 'http://local')
const app = (path: string) => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />)

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/experiments') return Response.json({ items: [experiment], next_cursor: null })
    if (path === '/api/runs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/runs/r1') return Response.json(running)
    if (path === '/api/runs/r1/results') return Response.json({ run_id: 'r1', state: 'succeeded', completed_through: '2024-01-03', metrics: { final_equity: 100 }, metric_definitions: { final_equity: { description: '最终权益' } }, warnings: [], metadata: { dataset_version_id: 'd1', strategy_revision_id: 's1', price_mode: 'raw_cash' }, detail_url: '/runs/r1' })
    if (requestUrl(input).pathname === '/api/runs/r1/logs') return Response.json({ items: [event], next_cursor: '1', state: 'running', completed_through: '2024-01-03' })
    if (requestUrl(input).pathname.startsWith('/api/runs/r1/tables/')) return Response.json(empty)
    if (requestUrl(input).pathname.startsWith('/api/runs/r1/charts/')) return Response.json({ items: [], total_rows: 0, sampled: false })
    if (path === '/api/runs' && init?.method === 'POST') return Response.json(queued)
    throw new Error(`unexpected ${init?.method || 'GET'} ${path}`)
  }))
})

test('retries submission with the same key after a transport failure', async () => {
  const requests: { idempotency_key: string; experiment_id: string; expected_revision: number }[] = []
  let attempts = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/runs' && init?.method === 'POST') {
      requests.push(JSON.parse(String(init.body)))
      if (++attempts === 1) throw new Error('network timeout')
      return Response.json(queued)
    }
    return prior(input, init)
  }))
  app('/runs')
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.click(screen.getByRole('button', { name: '运行' }))
  await userEvent.click(await screen.findByRole('button', { name: '重试提交' }))
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[0].idempotency_key).toBe(requests[1].idempotency_key)
  expect(requests[0]).toMatchObject({ experiment_id: 'e1', expected_revision: 3 })
  expect(await screen.findByRole('link', { name: /r1/ })).toBeInTheDocument()
})

test('keeps cancellation pending until the server acknowledges a terminal state', async () => {
  let state: RunRecord = running as RunRecord
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/runs/r1' && !init?.method) return Response.json(state)
    if (String(input) === '/api/runs/r1/cancel') { state = { ...state, state: 'cancel_requested' }; return Response.json(state) }
    return prior(input, init)
  }))
  app('/runs/r1')
  await userEvent.click(await screen.findByRole('button', { name: '取消任务' }))
  expect(await screen.findAllByText('正在取消')).toHaveLength(2)
  expect(screen.queryByText('已取消')).toBeNull()
  state = { ...running, state: 'cancelled', finished_at: '2024-01-01T00:02:00+00:00' }
  await userEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  expect(await screen.findByText('已取消')).toBeVisible()
})

test('restores run state and the recent log tail after a page reload', async () => {
  const paths: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    paths.push(String(input))
    return prior(input, init)
  }))
  const first = app('/runs/r1')
  expect(await screen.findByText('已完成交易日')).toBeVisible()
  expect(screen.getByText(/3 \/ 10/)).toBeVisible()
  first.unmount()
  app('/runs/r1')
  expect(await screen.findByText('已完成交易日')).toBeVisible()
  expect(screen.getAllByText('已完成交易日')).toHaveLength(1)
  expect(paths.filter(path => path === '/api/runs/r1/logs?tail=true')).toHaveLength(2)
})

test('rejects an oversized parameter list before creating variants', async () => {
  const requests: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => { requests.push(`${init?.method || 'GET'} ${String(input)}`); return prior(input, init) }))
  app('/runs')
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(screen.getByLabelText('参数列表 JSON'), { target: { value: JSON.stringify(Array.from({ length: 101 }, (_, i) => ({ window: i + 1 }))) } })
  await userEvent.click(screen.getByRole('button', { name: '运行参数列表' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('最多 100 组')
  expect(requests.filter(path => path === 'POST /api/experiments')).toHaveLength(0)
})

test('validates every parameter set before creating any variant draft', async () => {
  const requests: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    requests.push(`${init?.method || 'GET'} ${path}`)
    if (path === '/api/strategies/s1') return Response.json({ id: 's1', manifest: { parameter_schema: { type: 'object', properties: { window: { type: 'integer', minimum: 1 } }, required: ['window'] } } })
    return prior(input, init)
  }))
  app('/runs')
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(screen.getByLabelText('参数列表 JSON'), { target: { value: '[{"window":4},{"window":0}]' } })
  await userEvent.click(screen.getByRole('button', { name: '运行参数列表' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('第 2 组')
  expect(requests).not.toContain('POST /api/experiments')
})

test('batch transport retry keeps ordered draft revisions and idempotency key', async () => {
  let draftNumber = 0
  const submissions: { items: { experiment_id: string; expected_revision: number }[]; idempotency_key: string }[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/strategies/s1') return Response.json({ id: 's1', manifest: { parameter_schema: { type: 'object', properties: { window: { type: 'integer', minimum: 1 } }, required: ['window'] } } })
    if (path === '/api/experiments' && init?.method === 'POST') return Response.json({ id: `variant-${++draftNumber}`, revision: 1, payload: JSON.parse(String(init.body)).payload })
    if (path === '/api/runs/batch') {
      submissions.push(JSON.parse(String(init?.body)))
      if (submissions.length === 1) throw new Error('network timeout')
      return Response.json({ items: [{ ...queued, run_id: 'r2', group_id: 'g1' }] })
    }
    return prior(input, init)
  }))
  app('/runs')
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(screen.getByLabelText('参数列表 JSON'), { target: { value: '[{"window":4},{"window":8}]' } })
  await userEvent.click(screen.getByRole('button', { name: '运行参数列表' }))
  await userEvent.click(await screen.findByRole('button', { name: '重试提交' }))
  await waitFor(() => expect(submissions).toHaveLength(2))
  expect(submissions[1]).toEqual(submissions[0])
  expect(submissions[0].items).toEqual([{ experiment_id: 'variant-1', expected_revision: 1 }, { experiment_id: 'variant-2', expected_revision: 1 }])
  expect(draftNumber).toBe(2)
})

test('does not duplicate final equity rows when refreshing a finished run', async () => {
  let equityReads = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runs/r1') return Response.json({ ...running, state: 'succeeded', finished_at: '2024-01-01T00:02:00+00:00' })
    const url = requestUrl(input)
    if (url.pathname === '/api/runs/r1/tables/equity') {
      expect(url.searchParams.get('date_from')).toBe('2024-01-03')
      expect(url.searchParams.get('date_to')).toBe('2024-01-03')
      expect(url.searchParams.get('limit')).toBe('100')
      equityReads++
      return Response.json({ items: [{ date: '2024-01-03', cash: 10, receivables: 0, market_value: 90, equity: 100, stale_value: 0 }], next_cursor: null, total: 1 })
    }
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByRole('cell', { name: '2024-01-03' })).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  await waitFor(() => expect(equityReads).toBe(2))
  expect(screen.getAllByRole('cell', { name: '2024-01-03' })).toHaveLength(1)
})

test('upserts a partial order by ID when its fill status changes', async () => {
  let status = 'pending'
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (requestUrl(input).pathname === '/api/runs/r1/tables/orders') return Response.json({ items: [{ id: 'o1', signal_date: '2024-01-03', execution_date: '2024-01-04', symbol: '000001', side: 'buy', status, reason: '目标仓位', source: 'strategy', quantity: 100, filled_quantity: status === 'filled' ? 100 : 0, expired_quantity: 0 }], next_cursor: null, total: 1 })
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByTitle('pending')).toHaveTextContent('待成交')
  status = 'filled'
  await userEvent.click(screen.getByRole('button', { name: '刷新状态' }))
  expect(await screen.findByTitle('filled')).toHaveTextContent('已成交')
  expect(screen.queryByTitle('pending')).toBeNull()
  expect(screen.getAllByText(/订单 o1/)).toHaveLength(1)
})

test('shows next-session fills beside the dated signal through their exact order ID', async () => {
  const prior = globalThis.fetch
  const traceRequests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    const url = requestUrl(input)
    if (url.pathname === '/api/runs/r1/tables/orders' || url.pathname === '/api/runs/r1/tables/fills') {
      traceRequests.push(path)
      if (url.searchParams.get('context_date') !== '2024-01-03' || url.searchParams.get('limit') !== '100') return Response.json(empty)
      if (url.pathname.endsWith('/orders')) return Response.json({ items: [{ id: 'o1', signal_date: '2024-01-03', execution_date: '2024-01-04', symbol: '000001', side: 'buy', status: 'filled', reason: '目标仓位', source: 'strategy', quantity: 100, filled_quantity: 100, expired_quantity: 0 }], next_cursor: null, total: 1 })
      return Response.json({ items: [{ order_id: 'o1', trade_date: '2024-01-04', symbol: '000001', side: 'buy', quantity: 100, price: 1, commission: 0, slippage_cost: 0 }], next_cursor: null, total: 1 })
    }
    return prior(input, init)
  }))
  app('/runs/r1')
  fireEvent.change(await screen.findByLabelText('模拟日期'), { target: { value: '2024-01-03' } })
  await userEvent.click(screen.getByRole('button', { name: '查询追踪' }))
  expect(screen.getByRole('link', { name: 'o1' })).toHaveAttribute('href', '#order-o1')
  expect(screen.getByText(/价格 1/)).toBeVisible()
  expect(traceRequests.some(path => requestUrl(path).pathname.endsWith('/fills') && requestUrl(path).searchParams.get('context_date') === '2024-01-03')).toBe(true)
})

const screenshotRun = { ...running, state: 'succeeded', completed_through: '2026-05-19', created_at: '2026-09-30T17:25:14.122820+00:00', started_at: '2026-09-30T17:25:35.757974+00:00', finished_at: '2026-09-30T17:25:45.832972+00:00' } as RunRecord

test('run progress shows Shanghai wall-clock times across midnight without shifting market dates', () => {
  const progress = { ...event, asof: '2026-05-19', completed_days: 330, total_days: 330 }
  render(<RunProgress run={screenshotRun} latest={progress} progress={progress} />)
  expect(screen.getByText(/提交：2026-10-01 01:25:14.*开始：2026-10-01 01:25:35.*结束：2026-10-01 01:25:45/)).toBeVisible()
  expect(screen.getByText(/北京时间.*Asia\/Shanghai/)).toBeVisible()
  expect(screen.getByText('模拟日期：2026-05-19 · 已完成至：2026-05-19')).toBeVisible()
  expect(screen.getByText('运行耗时：0时 0分 10秒')).toBeVisible()
  expect(screen.queryByText(/2026-09-30T17:/)).toBeNull()
})

test('run list uses the same Shanghai timezone as the detail progress', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/runs' && !init?.method
    ? Response.json({ items: [screenshotRun], next_cursor: null }) : prior(input, init)))
  app('/runs')
  expect(await screen.findByText('提交 2026-10-01 01:25:14')).toBeVisible()
  await userEvent.click(screen.getByText('参数与来源'))
  expect(screen.getByText(/开始 2026-10-01 01:25:35.*结束 2026-10-01 01:25:45/)).toBeVisible()
  expect(screen.getByText(/北京时间.*Asia\/Shanghai/)).toBeVisible()
  expect(screen.queryByText(/2026-09-30T17:/)).toBeNull()
})

test('keeps each run log tail independent when moving between detail URLs', async () => {
  const prior = globalThis.fetch
  const logPaths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runs/r2') return Response.json({ ...running, run_id: 'r2', job_id: 'j2', detail_url: '/runs/r2' })
    if (path.startsWith('/api/runs/r2/logs')) {
      logPaths.push(path)
      return Response.json({ items: [{ ...event, message: '第二次运行事件' }], next_cursor: '1', state: 'running', completed_through: '2024-01-03' })
    }
    if (path.startsWith('/api/runs/r2/tables/')) return Response.json(empty)
    return prior(input, init)
  }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/runs/r1'] })
  render(<RouterProvider router={router} />)
  expect(await screen.findByText('已完成交易日')).toBeVisible()
  await act(async () => { await router.navigate('/runs/r2') })
  expect(await screen.findByText('第二次运行事件')).toBeVisible()
  expect(logPaths[0]).toBe('/api/runs/r2/logs?tail=true')
})

test('drains later run pages and refreshes a visible active run by ID', async () => {
  const listed = Array.from({ length: 50 }, (_, index) => ({ ...queued, run_id: `old-${index}`, state: 'succeeded', detail_url: `/runs/old-${index}` }))
  const later = { ...running, run_id: 'later', detail_url: '/runs/later' }
  const paths: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    paths.push(path)
    if (path === '/api/runs') return Response.json({ items: listed, next_cursor: '50' })
    if (path === '/api/runs?cursor=50') return Response.json({ items: [later], next_cursor: null })
    if (path === '/api/runs?cursor=51') return Response.json(empty)
    if (path === '/api/runs/later') return Response.json({ ...later, state: 'cancel_requested' })
    if (path.startsWith('/api/runs/old-')) return path.endsWith('/results') ? Response.json({ run_id: path.split('/')[3], state: 'succeeded', metrics: { final_equity: 100 }, metric_definitions: {}, metadata: {}, detail_url: path }) : Response.json({ ...queued, run_id: path.split('/')[3], state: 'succeeded', provenance: { strategy_revision_id: 's1', dataset_version_id: 'd1', effective_strategy_params: {} } })
    return prior(input, init)
  }))
  app('/runs')
  expect(await screen.findByRole('link', { name: 'later' })).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '刷新' }))
  await waitFor(() => expect(paths).toContain('/api/runs/later'))
})

test('editing the selected experiment while strategy validation waits cannot submit the old draft', async () => {
  let releaseStrategy: ((response: Response) => void) | undefined
  let strategyReads = 0
  const submitted: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/strategies/s1') {
      if (++strategyReads === 1) return new Promise<Response>(resolve => { releaseStrategy = resolve })
      return Response.json({ manifest: { parameter_schema: { type: 'object', properties: { window: { type: 'integer' } } } } })
    }
    if (path === '/api/experiments' && init?.method === 'POST') return Response.json({ id: 'variant', revision: 1 })
    if (path === '/api/runs/batch') { submitted.push(String(init?.body)); return Response.json({ items: [{ ...queued, run_id: 'new' }] }) }
    return prior(input, init)
  }))
  app('/runs')
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(screen.getByLabelText('参数列表 JSON'), { target: { value: '[{"window":4}]' } })
  await userEvent.click(screen.getByRole('button', { name: '运行参数列表' }))
  await waitFor(() => expect(releaseStrategy).toBeDefined())
  await userEvent.selectOptions(screen.getByLabelText('实验草稿'), '')
  releaseStrategy!(Response.json({ manifest: { parameter_schema: { type: 'object', properties: { window: { type: 'integer' } } } } }))
  await waitFor(() => expect(screen.getByRole('button', { name: '运行参数列表' })).toBeEnabled())
  expect(submitted).toHaveLength(0)
  await userEvent.selectOptions(screen.getByLabelText('实验草稿'), 'e1')
  await userEvent.click(screen.getByRole('button', { name: '运行参数列表' }))
  await waitFor(() => expect(submitted).toHaveLength(1))
})

test('editing parameters during variant creation releases the lock without submitting stale items', async () => {
  let releaseVariant: ((response: Response) => void) | undefined
  let variants = 0
  const submitted: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/strategies/s1') return Response.json({ manifest: { parameter_schema: { type: 'object', properties: { window: { type: 'integer' } } } } })
    if (path === '/api/experiments' && init?.method === 'POST') {
      if (++variants === 1) return new Promise<Response>(resolve => { releaseVariant = resolve })
      return Response.json({ id: `variant-${variants}`, revision: 1 })
    }
    if (path === '/api/runs/batch') { submitted.push(String(init?.body)); return Response.json({ items: [{ ...queued, run_id: 'new' }] }) }
    return prior(input, init)
  }))
  app('/runs')
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(screen.getByLabelText('参数列表 JSON'), { target: { value: '[{"window":4}]' } })
  await userEvent.click(screen.getByRole('button', { name: '运行参数列表' }))
  await waitFor(() => expect(releaseVariant).toBeDefined())
  fireEvent.change(screen.getByLabelText('参数列表 JSON'), { target: { value: '[{"window":8}]' } })
  releaseVariant!(Response.json({ id: 'stale-variant', revision: 1 }))
  await waitFor(() => expect(screen.getByRole('button', { name: '运行参数列表' })).toBeEnabled())
  expect(submitted).toHaveLength(0)
  await userEvent.click(screen.getByRole('button', { name: '运行参数列表' }))
  await waitFor(() => expect(submitted).toHaveLength(1))
  expect(JSON.parse(submitted[0]).items).toEqual([{ experiment_id: 'variant-2', expected_revision: 1 }])
})

test('refresh after complete pagination does not skip a second append after exactly 50 runs', async () => {
  const records = Array.from({ length: 50 }, (_, index) => ({ ...queued, run_id: `old-${index}`, state: 'succeeded', detail_url: `/runs/old-${index}` }))
  const paths: string[] = []
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    paths.push(path)
    if (path === '/api/runs' || path.startsWith('/api/runs?cursor=')) {
      const offset = Number(new URL(path, 'http://local').searchParams.get('cursor') || 0)
      return Response.json({ items: records.slice(offset, offset + 50), next_cursor: records.length > offset + 50 ? String(offset + 50) : null })
    }
    return prior(input, init)
  }))
  app('/runs')
  expect(await screen.findByRole('link', { name: 'old-49' })).toBeInTheDocument()
  records.push({ ...queued, run_id: 'new-51', state: 'succeeded', detail_url: '/runs/new-51' })
  await userEvent.click(screen.getByRole('button', { name: '刷新' }))
  expect(await screen.findByRole('link', { name: 'new-51' })).toBeInTheDocument()
  records.push({ ...queued, run_id: 'new-52', state: 'succeeded', detail_url: '/runs/new-52' })
  await userEvent.click(screen.getByRole('button', { name: '刷新' }))
  expect(await screen.findByRole('link', { name: 'new-52' })).toBeInTheDocument()
  expect(paths).toContain('/api/runs?cursor=51')
})

test('shows only the latest 100 terminal log events without draining earlier pages', async () => {
  const prior = globalThis.fetch
  const paths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runs/r1') return Response.json({ ...running, state: 'succeeded', finished_at: '2024-01-01T00:02:00+00:00' })
    if (requestUrl(input).pathname === '/api/runs/r1/logs') {
      paths.push(path)
      const url = requestUrl(input)
      expect(url.searchParams.get('tail')).toBe('true')
      expect(url.searchParams.has('cursor')).toBe(false)
      const items = Array.from({ length: 100 }, (_, index) => ({ ...event, seq: index + 106, message: `事件 ${index + 106}` }))
      return Response.json({ items, next_cursor: '205', state: 'succeeded', completed_through: '2024-01-03' })
    }
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText('事件 205')).toBeVisible()
  expect(screen.getByText('事件 106')).toBeVisible()
  expect(screen.queryByText('事件 105')).toBeNull()
  expect(paths).toEqual(['/api/runs/r1/logs?tail=true'])
})

test('worker completion keeps the last known day count and completion phase', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runs/r1') return Response.json({ ...running, state: 'succeeded', finished_at: '2024-01-01T00:02:00+00:00' })
    if (path === '/api/runs/r1/logs?tail=true') return Response.json({ items: [
      { ...event, seq: 1, phase: 'worker', asof: null, completed_days: 0, total_days: 0, code: 'WORKER_STARTED', message: '工作进程已启动' },
      { ...event, seq: 2, phase: 'backtest', code: 'DAY_COMPLETED', message: '交易日计算完成' },
      { ...event, seq: 3, phase: 'worker', asof: null, completed_days: 0, total_days: 0, code: 'WORKER_COMPLETED', message: '任务计算和产物校验已完成' }
    ], next_cursor: '3', state: 'succeeded', completed_through: '2024-01-03' })
    return prior(input, init)
  }))
  app('/runs/r1')
  expect(await screen.findByText(/已完成交易日 3 \/ 10/)).toBeVisible()
  expect(screen.getByText(/阶段：任务完成/)).toBeVisible()
  expect(screen.getByRole('progressbar', { name: '交易日进度' })).toHaveAttribute('value', '3')
})

test('aborts a pending manual refresh on navigation', async () => {
  let detailReads = 0
  let pendingSignal: AbortSignal | undefined
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/runs/r1' && ++detailReads === 2) {
      pendingSignal = init?.signal as AbortSignal
      return new Promise<Response>(() => {})
    }
    return prior(input, init)
  }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/runs/r1'] })
  render(<RouterProvider router={router} />)
  await userEvent.click(await screen.findByRole('button', { name: '刷新状态' }))
  await waitFor(() => expect(pendingSignal).toBeDefined())
  await act(async () => { await router.navigate('/runs') })
  expect(pendingSignal?.aborted).toBe(true)
})

test('aborts cancellation follow-up GET on navigation while preserving the POST outcome', async () => {
  let detailReads = 0
  let pendingSignal: AbortSignal | undefined
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/runs/r1/cancel') return Response.json({ ...running, state: 'cancel_requested' })
    if (path === '/api/runs/r1' && ++detailReads === 2) {
      pendingSignal = init?.signal as AbortSignal
      return new Promise<Response>(() => {})
    }
    return prior(input, init)
  }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/runs/r1'] })
  render(<RouterProvider router={router} />)
  await userEvent.click(await screen.findByRole('button', { name: '取消任务' }))
  await waitFor(() => expect(pendingSignal).toBeDefined())
  await act(async () => { await router.navigate('/runs') })
  expect(pendingSignal?.aborted).toBe(true)
})
