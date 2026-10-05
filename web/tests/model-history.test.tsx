import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { ModelDetailPage } from '../src/pages/ModelDetailPage'

const spec = { name: 'ETF model', frequency: 'weekly', backtest: { symbols: ['ETF1'] }, validation_start: '2024-01-01', validation_end: '2024-12-31', train_start: '2022-01-01', train_end: '2023-12-31', test_start: '2025-01-01', test_end: '2025-12-31', total_timesteps: 4096, device: 'cpu', selection_metric: 'validation_sharpe' }
const summary = { id: 'm1', name: 'ETF model', dataset_id: 'd1', spec, state: 'ready', active_job_id: null, active_session: null, total_timesteps: 4096, session_count: 305, version_count: 305, latest_version_id: 'job-305', created_at: '2024-01-01T00:00:00Z' }
const item = (round: number) => ({ id: `job-${round}`, session_number: round, round_number: round, base_version_id: null, base_round_number: null, state: 'succeeded', created_at: '2025-01-01T00:00:00Z', started_at: null, finished_at: null, heartbeat: null, completed_through: null, error: null, timesteps_completed: round * 512, session_timesteps: 512, best_timesteps: 512, best_round_number: round, best_source_version_id: null, runtime_device: 'cpu', selection_valid: true, validation_latest: { sharpe: 1 }, validation_best: { sharpe: round / 100 } })
const calls: string[] = []
function renderPage() {
  const router = createMemoryRouter([{ path: '/models/:id', element: <ModelDetailPage /> }], { initialEntries: ['/models/m1'] })
  return { ...render(<RouterProvider router={router} />), router }
}
function page(rounds: number[], request: URL) {
  const pageSize = Number(request.searchParams.get('page_size') || 10)
  const pageNumber = Number(request.searchParams.get('page') || 1)
  const all = Array.from({ length: 305 }, (_, index) => item(305 - index))
  const targetRound = Number(request.searchParams.get('round_number'))
  const pageForLookup = targetRound ? Math.floor((305 - targetRound) / pageSize) + 1 : pageNumber
  const items = rounds.length ? rounds.map(round => item(round)) : targetRound ? all.slice((pageForLookup - 1) * pageSize, pageForLookup * pageSize) : all.slice((pageNumber - 1) * pageSize, pageNumber * pageSize)
  return { items, total: 305, page: pageForLookup, page_size: pageSize, total_pages: Math.ceil(305 / pageSize), anchor: 900 }
}
beforeEach(() => {
  calls.length = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = String(input); calls.push(raw)
    if (raw === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    const url = new URL(raw, 'http://local')
    if (url.pathname === '/api/investing/overview') return Response.json({ capabilities: { cuda_available: false }, datasets: [], policies: [], jobs: [], backtests: [] })
    if (url.pathname === '/api/investing/models/m1') return Response.json(summary)
    if (url.pathname === '/api/investing/models/m1/history') return Response.json(page([], url))
    if (url.pathname === '/api/investing/policies/job-53') return Response.json({ id: 'job-53', kind: 'policy', created_at: '2025-01-01', job_id: 'job-53', state: 'succeeded', manifest: { selection_valid: true, metrics: { validation: { sharpe: 0.53 } }, history: [] } })
    if (url.pathname === '/api/jobs/job-53/logs') return Response.json({ items: [], next_cursor: null, state: 'succeeded', completed_through: null })
    throw new Error(`Unexpected ${raw}`)
  }))
})

test('shows one paginated training history containing saved rounds', async () => {
  renderPage()
  expect(await screen.findByText('第305轮')).toBeVisible()
  expect(screen.getAllByText('第1页 / 共31页').length).toBeGreaterThan(0)
  const history = await screen.findByRole('region', { name: '训练历史' })
  expect(screen.queryByRole('region', { name: '训练会话' })).toBeNull()
  await userEvent.click(within(history).getAllByRole('button', { name: '下一页' })[0])
  await waitFor(() => expect(screen.getAllByText('第2页 / 共31页').length).toBeGreaterThan(0))
  expect(calls.some(path => path.includes('/history?kind=sessions&page=2&page_size=10&anchor=900'))).toBe(true)
  expect(screen.getAllByLabelText('训练历史每页条数')[0]).toHaveValue('10')
  expect(calls.filter(path => path.includes('/history?kind=versions'))).toHaveLength(1)
})

test('detail shows the configured reward and states when no holdout test period is set', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local')
    if (url.pathname === '/api/investing/models/m1') return Response.json({ ...summary, spec: { ...spec, reward_version: 'differential-sharpe-v1', test_start: null, test_end: null } })
    return prior(input, init)
  }))
  renderPage()
  expect(await screen.findByText(/奖励目标：Differential Sharpe Ratio（DSR）/)).toBeVisible()
  expect(screen.getByText((_, element) => element?.tagName === 'P' && element.textContent?.includes('未设置留出测试期') === true)).toBeVisible()
})

test('detail treats a missing reward version as the legacy net log return', async () => {
  renderPage()
  expect(await screen.findByText(/奖励目标：净对数收益（现有）/)).toBeVisible()
})

test('successful attempts show current-round performance and snapshot changes before technical details', async () => {
  const prior = globalThis.fetch
  const rows = [
    { ...item(305), best_round_number: 244, validation_latest: { total_return: .1234, max_drawdown: .0876, sharpe: 1.2345 }, validation_best: { total_return: .9, max_drawdown: .01, sharpe: 9 } },
    { ...item(244), validation_latest: { total_return: 0, max_drawdown: 0, sharpe: null } },
    { ...item(3), selection_valid: false, validation_latest: {} },
    { ...item(2), best_round_number: null },
    item(1),
  ]
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://local')
    if (url.pathname.endsWith('/history') && url.searchParams.get('kind') === 'sessions') return Response.json({ items: rows, total: 5, page: 1, page_size: 10, total_pages: 1, anchor: 900 })
    if (url.pathname === '/api/investing/policies/job-305') return Response.json({ id: 'job-305', manifest: {} })
    return prior(input, init)
  }))
  renderPage()
  const latest = (await screen.findByText('第305轮')).closest('article')!
  const metrics = within(latest).getByRole('definition', { name: '总收益率' })
  expect(metrics).toHaveTextContent('12.34%')
  expect(within(latest).getByRole('definition', { name: '最大回撤（日末）' })).toHaveTextContent('8.76%')
  expect(within(latest).getByRole('definition', { name: '夏普比率（日末）' })).toHaveTextContent('1.2345')
  expect(within(latest).queryByText('90.00%')).toBeNull()
  expect(within(latest).getByText('未切换 · 沿用第244轮')).toBeVisible()
  expect(within(latest).queryByText(/累计步数|实际设备/)).toBeNull()
  const updated = screen.getByText('第244轮').closest('article')!
  expect(within(updated).getByText('本轮更新最优快照')).toBeVisible()
  expect(within(updated).getByRole('definition', { name: '总收益率' })).toHaveTextContent('0.00%')
  expect(within(updated).getByRole('definition', { name: '夏普比率（日末）' })).toHaveTextContent('—')
  expect(within(screen.getByText('第3轮').closest('article')!).getByText('暂无有效最优快照')).toBeVisible()
  expect(within(screen.getByText('第2轮').closest('article')!).getByText('最优快照来源未记录')).toBeVisible()
  expect(within(screen.getByText('第1轮').closest('article')!).getByText('首次建立最优快照')).toBeVisible()
  await userEvent.click(within(latest).getByRole('button', { name: '展开详情' }))
  expect(within(latest).getByText(/累计步数 156160 · 实际设备 CPU/)).toBeVisible()
  expect(within(latest).getByText('90.00%')).toBeVisible()
})

test('locates round 53 and lazily loads its artifact only after expansion', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = String(input); calls.push(raw)
    if (raw.endsWith('/api/session')) return Response.json({ token: 'token', api_base: '/api' })
    const url = new URL(raw, 'http://local')
    if (url.pathname === '/api/investing/overview') return Response.json({ capabilities: { cuda_available: false }, datasets: [], policies: [], jobs: [], backtests: [] })
    if (url.pathname.endsWith('/models/m1')) return Response.json(summary)
    if (url.pathname.endsWith('/history')) return Response.json(page([], url))
    if (url.pathname === '/api/investing/policies/job-53') return Response.json({ id: 'job-53', kind: 'policy', created_at: '2025-01-01', job_id: 'job-53', state: 'succeeded', manifest: { selection_valid: true, metrics: { validation: { sharpe: 0.53 } }, history: [] } })
    throw new Error(`Unexpected ${raw}`)
  }))
  renderPage()
  await screen.findByText('第305轮')
  fireEvent.change(screen.getByLabelText('查找训练轮次'), { target: { value: '53' } })
  await userEvent.click(screen.getByRole('button', { name: '查找轮次' }))
  const target = await screen.findByText('第53轮')
  expect(target).toBeVisible()
  const current = screen.getByRole('region', { name: '当前选定快照' })
  expect(within(current).getByText('第305轮快照')).toBeVisible()
  expect(within(current).getByRole('link', { name: '使用当前快照回测' })).toHaveAttribute('href', '/runs?model=m1&version=job-305')
  expect(calls.some(path => path.includes('round_number=53'))).toBe(true)
  await waitFor(() => expect(calls).toContain('/api/investing/policies/job-53'))
  expect(await screen.findByText('验证评估记录与版本完整信息')).toBeVisible()
  fireEvent.change(screen.getByLabelText('查找训练轮次'), { target: { value: '244' } })
  await userEvent.click(screen.getByRole('button', { name: '查找轮次' }))
  expect(await screen.findByText('第244轮')).toBeVisible()
  expect(calls.some(path => path.includes('round_number=244'))).toBe(true)
})

test('page size preserves the first visible version and the last page is reachable', async () => {
  renderPage()
  await screen.findByText('第305轮')
  const panel = screen.getByRole('region', { name: '训练历史' })
  fireEvent.change(within(panel).getByLabelText('查找训练轮次'), { target: { value: '53' } })
  await userEvent.click(within(panel).getByRole('button', { name: '查找轮次' }))
  expect(await within(panel).findByText('第53轮')).toBeVisible()
  await userEvent.selectOptions(within(panel).getAllByLabelText('训练历史每页条数')[0], '20')
  expect(await within(panel).findByText('第53轮')).toBeVisible()
  expect(calls.some(path => path.includes('page=13&page_size=20&anchor=900'))).toBe(true)
  const pageInput = within(panel).getByLabelText('训练历史跳转页码')
  fireEvent.change(pageInput, { target: { value: '16' } })
  await userEvent.click(within(panel).getAllByRole('button', { name: '跳转' })[0])
  expect(await within(panel).findByText('第5轮')).toBeVisible()
  expect(within(panel).getAllByText(/第16页 \/ 共16页/).length).toBeGreaterThan(0)
})

test('failed page navigation keeps the visible page and page size for retry', async () => {
  let pageTwoFailures = 0, pageSizeFailures = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = String(input); calls.push(raw)
    if (raw.endsWith('/api/session')) return Response.json({ token: 'token', api_base: '/api' })
    const url = new URL(raw, 'http://local')
    if (url.pathname === '/api/investing/overview') return Response.json({ capabilities: { cuda_available: false }, datasets: [], policies: [], jobs: [], backtests: [] })
    if (url.pathname.endsWith('/models/m1') && url.searchParams.get('include_history') === 'false') return Response.json(summary)
    if (url.pathname.endsWith('/history')) {
      if (url.searchParams.get('kind') === 'sessions' && url.searchParams.get('page') === '2' && pageTwoFailures++ === 0) return Response.json({ code: 'TEMPORARY', message: 'temporary page failure' }, { status: 503 })
      if (url.searchParams.get('kind') === 'sessions' && url.searchParams.get('page_size') === '50' && pageSizeFailures++ === 0) return Response.json({ code: 'TEMPORARY', message: 'temporary page-size failure' }, { status: 503 })
      return Response.json(page([], url))
    }
    throw new Error(`Unexpected ${raw}`)
  }))
  renderPage()
  await screen.findByText('第305轮')
  const panel = screen.getByRole('region', { name: '训练历史' })
  expect(within(panel).getByText('第305轮')).toBeVisible()
  await userEvent.click(within(panel).getAllByRole('button', { name: '下一页' })[0])
  expect(await within(panel).findByRole('alert')).toHaveTextContent('temporary page failure')
  expect(within(panel).getAllByText(/第1页 \/ 共31页/).length).toBeGreaterThan(0)
  expect(within(panel).getByLabelText('训练历史每页条数')).toHaveValue('10')
  await userEvent.click(within(panel).getByRole('button', { name: '重试加载' }))
  expect(await within(panel).findByText('第295轮')).toBeVisible()
  expect(within(panel).getAllByText(/第2页 \/ 共31页/).length).toBeGreaterThan(0)
  await userEvent.selectOptions(within(panel).getByLabelText('训练历史每页条数'), '50')
  expect(await within(panel).findByRole('alert')).toHaveTextContent('temporary page-size failure')
  expect(within(panel).getByLabelText('训练历史每页条数')).toHaveValue('10')
  expect(within(panel).getByText('第295轮')).toBeVisible()
  await userEvent.click(within(panel).getByRole('button', { name: '重试加载' }))
  expect(await within(panel).findByText('第305轮')).toBeVisible()
  expect(within(panel).getByLabelText('训练历史每页条数')).toHaveValue('50')
})

test('failed attempts appear in training history without a saved round or validation metrics', async () => {
  const failure = { ...item(3), id: 'failed-job', state: 'failed', round_number: null, error: 'CUDA unavailable' }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = String(input)
    if (raw.endsWith('/api/session')) return Response.json({ token: 'token', api_base: '/api' })
    const url = new URL(raw, 'http://local')
    if (url.pathname === '/api/investing/overview') return Response.json({ capabilities: { cuda_available: false }, datasets: [], policies: [], jobs: [], backtests: [] })
    if (url.pathname.endsWith('/models/m1')) return Response.json({ ...summary, session_count: 3, version_count: 2, latest_version_id: 'job-2' })
    if (url.pathname.endsWith('/history')) return Response.json(url.searchParams.get('kind') === 'sessions' ? { items: [failure, item(2), item(1)], total: 3, page: 1, page_size: 10, total_pages: 1, anchor: 900 } : { items: [item(2)], total: 2, page: 1, page_size: 1, total_pages: 2, anchor: 900 })
    throw new Error(`Unexpected ${raw}`)
  }))
  renderPage()
  const history = await screen.findByRole('region', { name: '训练历史' })
  const failed = (await within(history).findByText('第3次训练尝试')).closest('article')!
  expect(within(failed).getByText(/失败或中断原因：CUDA unavailable/)).toBeVisible()
  expect(within(failed).queryByText(/第\d+轮|总收益率|使用此版本回测/)).toBeNull()
})

test('log selection survives history navigation and model changes clear expanded state', async () => {
  const attempt = { ...item(306), id: 'attempt-306', round_number: null, state: 'running' }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const raw = String(input); calls.push(raw)
    if (raw.endsWith('/api/session')) return Response.json({ token: 'token', api_base: '/api' })
    const url = new URL(raw, 'http://local')
    if (url.pathname === '/api/investing/models/m1' && url.searchParams.get('include_history') === 'false') return Response.json({ ...summary, session_count: 306, active_job_id: attempt.id, active_session: attempt })
    if (url.pathname === '/api/investing/models/m2' && url.searchParams.get('include_history') === 'false') return Response.json({ ...summary, id: 'm2', name: 'Second model', session_count: 0, version_count: 0, latest_version_id: null })
    if (url.pathname.endsWith('/history')) {
      if (url.pathname.includes('/m2/')) return Response.json({ items: [], total: 0, page: 1, page_size: 10, total_pages: 1, anchor: 900 })
      if (url.searchParams.get('kind') === 'versions') return Response.json(page([], url))
      const pageNumber = Number(url.searchParams.get('page'))
      const all = [attempt, ...Array.from({ length: 305 }, (_, index) => item(305 - index))]
      return Response.json({ ...page([], url), items: all.slice((pageNumber - 1) * 10, pageNumber * 10), total: 306 })
    }
    if (url.pathname === '/api/jobs/attempt-306/logs') return Response.json({ items: [], next_cursor: null, state: 'running', completed_through: null })
    throw new Error(`Unexpected ${raw}`)
  }))
  const view = renderPage()
  await screen.findByText('第305轮')
  await userEvent.click(within(screen.getByText('第306次训练尝试').closest('article')!).getByRole('button', { name: '查看日志' }))
  expect(await screen.findByRole('heading', { name: '第306次尝试 · 训练日志' })).toBeVisible()
  const history = screen.getByRole('region', { name: '训练历史' })
  await userEvent.click(within(history).getAllByRole('button', { name: '下一页' })[0])
  expect(await within(history).findByText('第296轮')).toBeVisible()
  expect(screen.getByRole('heading', { name: '第306次尝试 · 训练日志' })).toBeVisible()
  await view.router.navigate('/models/m2')
  expect(await screen.findByRole('heading', { name: 'Second model' })).toBeVisible()
  expect(screen.queryByRole('heading', { name: '第306次尝试 · 训练日志' })).toBeNull()
  expect(screen.queryByText('第296轮')).toBeNull()
})
