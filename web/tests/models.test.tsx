import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'

const spec = { name: '研究模型', frequency: 'weekly', backtest: { symbols: ['ETF2', 'ETF1'] }, validation_end: '2023-12-31', test_start: '2024-01-01', test_end: '2024-12-31', total_timesteps: 4096, device: 'cpu', selection_metric: 'validation_sharpe' }
const model = { id: 'm1', name: '研究模型', dataset_id: 'd1', spec, state: 'ready', active_job_id: null, total_timesteps: 4096, session_count: 1, version_count: 1, latest_version_id: 'v1', created_at: '2024-01-01T00:00:00Z', sessions: [], versions: [{ id: 'v1', round_number: 1, best_round_number: 1, created_at: '2024-01-01T00:00:00Z', manifest: { metrics: { validation: { total_return: .1, sharpe: null, max_drawdown: -.1 } }, timesteps_completed: 4096 } }] }
const datasets = [{ id: 'd1', manifest: { data_start: '2020-01-01', data_end: '2025-12-31', price_mode: 'adjusted_research', limitations: [] } }]
const posts: { path: string; body: any }[] = []
const savedVersion = { id: 'v1', round_number: 1, session_number: 1, state: 'succeeded', created_at: '2024-01-01T00:00:00Z', timesteps_completed: 4096, runtime_device: 'cpu', selection_valid: true, validation_best: { total_return: .1, sharpe: null, max_drawdown: -.1 }, validation_latest: {} }
let versionItems: Record<string, unknown>[] = []
const app = (path: string) => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />)
beforeEach(() => {
  posts.length = 0
  versionItems = [savedVersion]
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    if (init?.method === 'POST') { posts.push({ path, body: JSON.parse(String(init.body)) }); return Response.json(path === '/api/investing/models' ? { ...model, state: 'pending', versions: [] } : { id: 'j1', state: 'queued' }) }
    if (path.startsWith('/api/investing/models/m1/history?')) {
      const items = versionItems
      return Response.json({ items, total: items.length, page: 1, page_size: 10, total_pages: 1, anchor: 10 })
    }
    if (path === '/api/investing/models/m1?include_history=false') return Response.json({ ...model, active_session: null, capabilities: { cuda_available: false } })
    if (path.startsWith('/api/investing/policies/')) return Response.json(model.versions[0])
    if (path === '/api/investing/models/m1') return Response.json(model)
    if (path.startsWith('/api/investing/models')) return Response.json({ items: [model], next_cursor: null })
    if (path === '/api/investing/overview') return Response.json({ datasets, policies: [], jobs: [], backtests: [], capabilities: { cuda_available: false } })
    if (path === '/api/backtests/catalog?limit=50' || path === '/api/experiments') return Response.json({ items: [], next_cursor: null })
    throw new Error(`Unexpected ${path}`)
  }))
})
afterEach(() => vi.useRealTimers())

test('has exactly four domain links and opens models before creation', async () => {
  app('/models')
  expect(within(screen.getByRole('navigation', { name: '主导航' })).getAllByRole('link').map(link => link.textContent)).toEqual(['行情数据', '固定策略', '强化学习模型', '回测'])
  expect(await screen.findByRole('link', { name: '研究模型' })).toHaveAttribute('href', '/models/m1')
  expect(screen.queryByLabelText('模型名称')).toBeNull()
})

test('creation requires an explicit ordered basket and creates without training', async () => {
  app('/models')
  await userEvent.click(await screen.findByRole('button', { name: '新建模型' }))
  await screen.findByRole('option', { name: /d1/ })
  expect(screen.getByLabelText('固定 ETF 篮子（按顺序，逗号分隔）')).toHaveValue('')
  await userEvent.selectOptions(screen.getByLabelText('日线数据版本'), 'd1')
  const dates = ['2021-01-01', '2022-12-31', '2023-01-01', '2023-12-31', '2024-01-01', '2024-12-31']
  ;['训练开始', '训练结束', '验证开始', '验证结束', '测试开始', '测试结束'].forEach((label, index) => fireEvent.change(screen.getByLabelText(label), { target: { value: dates[index] } }))
  await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('篮子')
  expect(posts).toHaveLength(0)
  fireEvent.change(screen.getByLabelText('固定 ETF 篮子（按顺序，逗号分隔）'), { target: { value: 'ETF2,ETF1' } })
  await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0]).toMatchObject({ path: '/api/investing/models', body: { dataset_id: 'd1', spec: { action_version: 'hold-target-cash-quartiles-v2', selection_metric: 'validation_sharpe', backtest: { symbols: ['ETF2', 'ETF1'] } } } })
  expect(posts.some(item => item.path.endsWith('/train'))).toBe(false)
})

test('creation defaults to the legacy reward and allows training without a holdout test period', async () => {
  app('/models')
  await userEvent.click(await screen.findByRole('button', { name: '新建模型' }))
  await userEvent.selectOptions(await screen.findByLabelText('日线数据版本'), 'd1')
  fireEvent.change(screen.getByLabelText('训练开始'), { target: { value: '2021-01-01' } })
  fireEvent.change(screen.getByLabelText('训练结束'), { target: { value: '2022-12-31' } })
  fireEvent.change(screen.getByLabelText('验证开始'), { target: { value: '2023-01-01' } })
  fireEvent.change(screen.getByLabelText('验证结束'), { target: { value: '2023-12-31' } })
  fireEvent.change(screen.getByLabelText('固定 ETF 篮子（按顺序，逗号分隔）'), { target: { value: 'ETF1' } })
  expect(screen.getByLabelText('奖励方案')).toHaveValue('net-log-return-v1')
  await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].body.spec).toMatchObject({ reward_version: 'net-log-return-v1', seed: 42, backtest: { end: '2023-12-31' } })
  expect(posts[0].body.spec).not.toHaveProperty('test_start')
  expect(posts[0].body.spec).not.toHaveProperty('test_end')
})

test('creation rejects a partial holdout test period', async () => {
  app('/models')
  await userEvent.click(await screen.findByRole('button', { name: '新建模型' }))
  await userEvent.selectOptions(await screen.findByLabelText('日线数据版本'), 'd1')
  ;[['训练开始', '2021-01-01'], ['训练结束', '2022-12-31'], ['验证开始', '2023-01-01'], ['验证结束', '2023-12-31'], ['测试开始', '2024-01-01'], ['固定 ETF 篮子（按顺序，逗号分隔）', 'ETF1']].forEach(([label, value]) => fireEvent.change(screen.getByLabelText(label), { target: { value } }))
  await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('测试开始和结束日期必须同时填写')
  expect(posts).toHaveLength(0)
})

test('new reward choices enforce gamma one and zero risk-free rate', async () => {
  app('/models')
  await userEvent.click(await screen.findByRole('button', { name: '新建模型' }))
  const reward = await screen.findByLabelText('奖励方案')
  expect(within(reward).getByRole('option', { name: /净对数收益 \+ 状态输入.*研究对照/ })).toBeInTheDocument()
  expect(within(reward).getByRole('option', { name: /Differential Sharpe.*DSR/ })).toBeInTheDocument()
  await userEvent.selectOptions(reward, 'differential-sharpe-v1')
  await userEvent.click(screen.getByText('高级训练、成交与风险参数'))
  expect(screen.getByLabelText('奖励折扣')).toHaveValue(1)
  expect(screen.getByLabelText('奖励折扣')).toBeDisabled()
  expect(screen.getByLabelText('随机种子')).toHaveValue(42)
  fireEvent.change(screen.getByLabelText('随机种子'), { target: { value: '17' } })
  await userEvent.selectOptions(screen.getByLabelText('日线数据版本'), 'd1')
  ;[['训练开始', '2021-01-01'], ['训练结束', '2022-12-31'], ['验证开始', '2023-01-01'], ['验证结束', '2023-12-31'], ['固定 ETF 篮子（按顺序，逗号分隔）', 'ETF1']].forEach(([label, value]) => fireEvent.change(screen.getByLabelText(label), { target: { value } }))
  await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].body.spec).toMatchObject({ reward_version: 'differential-sharpe-v1', gamma: 1, seed: 17, backtest: { seed: 17, metrics: { risk_free_rate: 0 } } })
})

test('detail keeps configuration read only and adds only session budget and device', async () => {
  app('/models/m1')
  expect(await screen.findByText('ETF2 → ETF1')).toBeVisible()
  expect(screen.queryByLabelText('固定 ETF 篮子（按顺序，逗号分隔）')).toBeNull()
  fireEvent.change(screen.getByLabelText('追加决策步数'), { target: { value: '8192' } })
  await userEvent.click(screen.getByRole('button', { name: '继续训练' }))
  expect(posts[0]).toEqual({ path: '/api/investing/models/m1/train', body: { additional_timesteps: 8192, device: 'cpu' } })
  expect(screen.getByRole('link', { name: '使用此版本回测' })).toHaveAttribute('href', '/runs?model=m1&version=v1')
  expect(within(screen.getByRole('region', { name: '训练历史' })).getByRole('definition', { name: '夏普比率（日末）' })).toHaveTextContent('—')
})

test('backtest defaults to the current snapshot and submits its saved version', async () => {
  app('/runs?model=m1')
  await waitFor(() => expect(screen.getByLabelText('保存版本')).toHaveValue('v1'))
  await userEvent.click(screen.getByRole('button', { name: '提交模型回测' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0]).toEqual({ path: '/api/investing/models/m1/backtests', body: { dataset_id: 'd1', version_id: 'v1', spec: { start: '2024-01-01', end: '2024-12-31', baseline: 'policy', cost_multiplier: 1 } } })
})

test('backtest dates start blank when the selected model has no holdout test period', async () => {
  const prior = globalThis.fetch
  const noTestModel = { ...model, id: 'm2', name: '无留出期模型', spec: { ...spec, validation_end: '2023-12-31', test_start: null, test_end: null } }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/investing/models') return Response.json({ items: [model, noTestModel], next_cursor: null })
    if (path === '/api/investing/models/m2?include_history=false' || path === '/api/investing/models/m2') return Response.json(noTestModel)
    if (path.startsWith('/api/investing/models/m2/history?')) return Response.json({ items: [savedVersion], total_pages: 1, anchor: 10 })
    return prior(input, init)
  }))
  app('/runs?model=m1')
  await screen.findByRole('option', { name: /研究模型/ })
  expect(screen.getByLabelText('回测开始')).toHaveValue('2024-01-01')
  await userEvent.selectOptions(screen.getByLabelText('强化学习模型'), 'm2')
  await screen.findByRole('option', { name: /无留出期模型/ })
  await waitFor(() => expect(screen.getByLabelText('回测开始')).toHaveValue(''))
  expect(screen.getByLabelText('回测结束')).toHaveValue('')
})

test('backtest uses real history rounds and short labels while preserving historical selection', async () => {
  const prior = globalThis.fetch
  versionItems = [{ ...savedVersion, id: 'v2', round_number: 254, best_round_number: 116 }, { ...savedVersion, round_number: 253 }]
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/investing/models/m1?include_history=false'
    ? Response.json({ ...model, latest_version_id: 'v2' }) : prior(input, init)))
  app('/runs?model=m1&version=v1')
  expect(await screen.findByRole('option', { name: '第254轮（当前）' })).toBeVisible()
  expect(screen.getByRole('option', { name: '第253轮' })).toBeVisible()
  expect(screen.getByLabelText('保存版本')).toHaveValue('v1')
  await userEvent.selectOptions(screen.getByLabelText('保存版本'), 'v2')
  await userEvent.click(screen.getByRole('button', { name: '提交模型回测' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0].body.version_id).toBe('v2')
})

test('backtest loads all history pages with a fixed anchor before selecting the current version', async () => {
  const prior = globalThis.fetch
  const historyCalls: URL[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost')
    if (url.pathname === '/api/investing/models/m1/history') {
      historyCalls.push(url)
      const page = Number(url.searchParams.get('page'))
      return Response.json({ items: [{ ...savedVersion, id: page === 1 ? 'v2' : 'v1', round_number: page === 1 ? 51 : 1 }], total_pages: 2, anchor: 102 })
    }
    return prior(input, init)
  }))
  app('/runs?model=m1')
  expect(await screen.findByRole('option', { name: '第51轮（当前）' })).toBeVisible()
  expect(screen.getByRole('option', { name: '第1轮' })).toBeVisible()
  expect(screen.getByLabelText('保存版本')).toHaveValue('v2')
  expect(historyCalls.map(url => url.searchParams.get('page'))).toEqual(['1', '2'])
  expect(historyCalls[1].searchParams.get('anchor')).toBe('102')
})

test('switching models defaults to its own snapshot and untrained models require a saved version', async () => {
  const prior = globalThis.fetch
  const otherModel = { ...model, id: 'm2', name: '第二模型', latest_version_id: 'v2', versions: [{ ...model.versions[0], id: 'v2' }] }
  const untrainedModel = { ...model, id: 'm3', name: '未训练模型', latest_version_id: null, versions: [] }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/investing/models') return Response.json({ items: [model, otherModel, untrainedModel], next_cursor: null })
    if (path === '/api/investing/models/m2?include_history=false') return Response.json(otherModel)
    if (path === '/api/investing/models/m3?include_history=false') return Response.json(untrainedModel)
    if (path.startsWith('/api/investing/models/m2/history?')) return Response.json({ items: [{ ...savedVersion, id: 'v2' }], total_pages: 1, anchor: 10 })
    if (path.startsWith('/api/investing/models/m3/history?')) return Response.json({ items: [], total_pages: 1, anchor: 0 })
    return prior(input, init)
  }))
  app('/runs?model=m1&version=v1')
  await screen.findByRole('option', { name: /第二模型/ })
  await userEvent.selectOptions(screen.getByLabelText('强化学习模型'), 'm2')
  await waitFor(() => expect(screen.getByLabelText('保存版本')).toHaveValue('v2'))
  await userEvent.selectOptions(screen.getByLabelText('强化学习模型'), 'm3')
  await screen.findByText('此模型尚无成功保存版本，请先完成训练。')
  expect(screen.getByLabelText('保存版本')).toHaveValue('')
  await userEvent.click(screen.getByRole('button', { name: '提交模型回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('请选择属于此模型的保存版本')
  expect(posts).toHaveLength(0)
})

test('new session starts with model defaults and unscored versions disclose unavailable validation score', async () => {
  const prior = globalThis.fetch
  versionItems = [{ ...savedVersion, selection_valid: false }]
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/investing/models/m1?include_history=false'
    ? Response.json({ ...model, spec: { ...spec, total_timesteps: 128 }, versions: [{ ...model.versions[0], manifest: { ...model.versions[0].manifest, selection_valid: false } }] }) : prior(input, init)))
  app('/models/m1')
  await screen.findByText('ETF2 → ETF1')
  expect(screen.getByLabelText('追加决策步数')).toHaveValue(128)
  expect(within(screen.getByRole('region', { name: '训练历史' })).getByText(/无有效验证评分/)).toBeVisible()
})

test('active sessions block overlapping training and explain cancellation progress loss', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/investing/models/m1?include_history=false'
    ? Response.json({ ...model, active_job_id: 'active', sessions: [{ id: 'active', state: 'running', base_version_id: 'v1' }] }) : prior(input, init)))
  app('/models/m1')
  await screen.findByText('ETF2 → ETF1')
  expect(screen.getByRole('button', { name: '继续训练' })).toBeDisabled()
  expect(screen.getByText(/取消会丢弃本次未发布进度/)).toBeVisible()
  expect(screen.getByRole('link', { name: '使用此版本回测' })).toHaveAttribute('href', '/runs?model=m1&version=v1')
  await userEvent.click(screen.getByRole('button', { name: '取消当前训练' }))
  expect(posts[0].path).toBe('/api/jobs/active/cancel')
})

test('a deep link pins its specified version and changing models clears it', async () => {
  app('/runs?model=m1&version=v1')
  await screen.findByText(/固定篮子/)
  expect(screen.getByLabelText('保存版本')).toHaveValue('v1')
  await userEvent.selectOptions(screen.getByLabelText('强化学习模型'), '')
  expect(screen.getByLabelText('保存版本')).toHaveValue('')
})

test('version separates latest parameters from retained best and reports its actual device', async () => {
  versionItems = [{ ...savedVersion, round_number: 2, runtime_device: 'cuda', best_timesteps: 512, best_round_number: 1,
    validation_latest: { total_return: 0, sharpe: null, max_drawdown: 0, net_log_return: 0 } }]
  app('/models/m1')
  await screen.findByText('ETF2 → ETF1')
  const link = screen.getAllByRole('link', { name: '使用此版本回测' })[0]
  const version = link.closest('article')!
  await userEvent.click(within(version).getByRole('button', { name: '展开详情' }))
  const current = within(version).getByRole('heading', { name: '本轮最终参数验证' }).nextElementSibling!
  const best = within(version).getByRole('heading', { name: '选定快照验证' }).nextElementSibling!
  expect(current).toHaveTextContent('0.00%')
  expect(within(current as HTMLElement).getByText('夏普比率（日末）').nextElementSibling).toHaveTextContent('—')
  expect(best).toHaveTextContent('10.00%')
  expect(within(version).getByText(/实际设备 CUDA/)).toBeVisible()
  expect(within(version).getByText(/选定快照步数 512/)).toBeVisible()
  expect(within(version).getByText(/选定快照来源：第1轮/)).toBeVisible()
  expect(screen.getByLabelText('下次提交设备')).toHaveValue('cpu')
})

test('idle detail discovers external training and stops polling after unmount', async () => {
  vi.useFakeTimers()
  const prior = globalThis.fetch
  let calls = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) !== '/api/investing/models/m1?include_history=false') return prior(input, init)
    calls += 1
    return Response.json(calls === 1 ? model : { ...model, active_job_id: 'external', state: 'running' })
  }))
  let view: ReturnType<typeof app>
  await act(async () => { view = app('/models/m1') })
  expect(calls).toBe(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(calls).toBe(2)
  expect(screen.getByText(/当前会话 external/)).toBeVisible()
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(calls).toBe(3)
  view!.unmount()
  await act(async () => { await vi.advanceTimersByTimeAsync(30000) })
  expect(calls).toBe(3)
  expect(posts).toHaveLength(0)
})

test('detail retries a transient read error without submitting training', async () => {
  vi.useFakeTimers()
  const prior = globalThis.fetch
  let calls = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) !== '/api/investing/models/m1?include_history=false') return prior(input, init)
    calls += 1
    if (calls === 1) throw new Error('temporary timeout')
    return Response.json(model)
  }))
  await act(async () => { app('/models/m1') })
  expect(screen.getByRole('alert')).toHaveTextContent('temporary timeout')
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(calls).toBe(2)
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('ETF2 → ETF1')).toBeVisible()
  expect(posts).toHaveLength(0)
})

test('polling waits for a slow response instead of overlapping model requests', async () => {
  vi.useFakeTimers()
  const prior = globalThis.fetch
  let calls = 0, release: () => void = () => {}
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) !== '/api/investing/models/m1?include_history=false') return prior(input, init)
    calls += 1
    if (calls === 2) await new Promise<void>(resolve => { release = resolve })
    return Response.json(model)
  }))
  let view: ReturnType<typeof app>
  await act(async () => { view = app('/models/m1') })
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(calls).toBe(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(30000) })
  expect(calls).toBe(2)
  await act(async () => { release() })
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(calls).toBe(3)
  view!.unmount()
})

test('backtest submits a historical slice before the validation cutoff', async () => {
  app('/runs?model=m1')
  await waitFor(() => expect(screen.getByLabelText('保存版本')).toHaveValue('v1'))
  fireEvent.change(screen.getByLabelText('回测开始'), { target: { value: '2023-06-01' } })
  fireEvent.change(screen.getByLabelText('回测结束'), { target: { value: '2023-09-30' } })
  await userEvent.click(screen.getByRole('button', { name: '提交模型回测' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0]).toEqual({ path: '/api/investing/models/m1/backtests', body: { dataset_id: 'd1', version_id: 'v1', spec: { start: '2023-06-01', end: '2023-09-30', baseline: 'policy', cost_multiplier: 1 } } })
})
