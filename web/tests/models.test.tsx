import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'

const spec = { name: '研究模型', frequency: 'weekly', backtest: { symbols: ['ETF2', 'ETF1'] }, validation_end: '2023-12-31', test_start: '2024-01-01', test_end: '2024-12-31', total_timesteps: 4096, device: 'cpu', selection_metric: 'validation_sharpe' }
const model = { id: 'm1', name: '研究模型', dataset_id: 'd1', spec, state: 'ready', active_job_id: null, total_timesteps: 4096, session_count: 1, version_count: 1, latest_version_id: 'v1', created_at: '2024-01-01T00:00:00Z', sessions: [], versions: [{ id: 'v1', created_at: '2024-01-01T00:00:00Z', manifest: { metrics: { validation: { total_return: .1, sharpe: null, max_drawdown: -.1 } }, timesteps_completed: 4096 } }] }
const datasets = [{ id: 'd1', manifest: { data_start: '2020-01-01', data_end: '2025-12-31', price_mode: 'adjusted_research', limitations: [] } }]
const posts: { path: string; body: any }[] = []
const app = (path: string) => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />)
beforeEach(() => {
  posts.length = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    if (init?.method === 'POST') { posts.push({ path, body: JSON.parse(String(init.body)) }); return Response.json(path === '/api/investing/models' ? { ...model, state: 'pending', versions: [] } : { id: 'j1', state: 'queued' }) }
    if (path.startsWith('/api/investing/models/m1')) return Response.json(model)
    if (path.startsWith('/api/investing/models')) return Response.json({ items: [model], next_cursor: null })
    if (path === '/api/investing/overview') return Response.json({ datasets, policies: [], jobs: [], backtests: [], capabilities: { cuda_available: false } })
    if (path === '/api/backtests/catalog?limit=50' || path === '/api/experiments') return Response.json({ items: [], next_cursor: null })
    throw new Error(`Unexpected ${path}`)
  }))
})

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
  expect(posts[0]).toMatchObject({ path: '/api/investing/models', body: { dataset_id: 'd1', spec: { selection_metric: 'validation_sharpe', backtest: { symbols: ['ETF2', 'ETF1'] } } } })
  expect(posts.some(item => item.path.endsWith('/train'))).toBe(false)
})

test('detail keeps configuration read only and adds only session budget and device', async () => {
  app('/models/m1')
  expect(await screen.findByText('ETF2 → ETF1')).toBeVisible()
  expect(screen.queryByLabelText('固定 ETF 篮子（按顺序，逗号分隔）')).toBeNull()
  fireEvent.change(screen.getByLabelText('追加决策步数'), { target: { value: '8192' } })
  await userEvent.click(screen.getByRole('button', { name: '继续训练' }))
  expect(posts[0]).toEqual({ path: '/api/investing/models/m1/train', body: { additional_timesteps: 8192, device: 'cpu' } })
  expect(screen.getByRole('link', { name: '使用此版本回测' })).toHaveAttribute('href', '/runs?model=m1&version=v1')
  expect(screen.getByText('Sharpe（日末）').nextElementSibling).toHaveTextContent('—')
})

test('backtest requires explicit version instead of choosing the latest', async () => {
  app('/runs?model=m1')
  await screen.findByRole('option', { name: /研究模型/ })
  expect(screen.getByLabelText('保存版本')).toHaveValue('')
  await userEvent.click(screen.getByRole('button', { name: '提交模型回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('版本')
  expect(posts).toHaveLength(0)
  await userEvent.selectOptions(screen.getByLabelText('保存版本'), 'v1')
  await userEvent.click(screen.getByRole('button', { name: '提交模型回测' }))
  await waitFor(() => expect(posts).toHaveLength(1))
  expect(posts[0]).toEqual({ path: '/api/investing/models/m1/backtests', body: { dataset_id: 'd1', version_id: 'v1', spec: { start: '2024-01-01', end: '2024-12-31', baseline: 'policy', cost_multiplier: 1 } } })
})

test('new session starts with model defaults and unscored versions disclose unavailable validation score', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/investing/models/m1'
    ? Response.json({ ...model, spec: { ...spec, total_timesteps: 128 }, versions: [{ ...model.versions[0], manifest: { ...model.versions[0].manifest, selection_valid: false } }] }) : prior(input, init)))
  app('/models/m1')
  await screen.findByText('ETF2 → ETF1')
  expect(screen.getByLabelText('追加决策步数')).toHaveValue(128)
  expect(screen.getByText(/无有效验证评分/)).toBeVisible()
})

test('active sessions block overlapping training and explain cancellation progress loss', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input) === '/api/investing/models/m1'
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
  await screen.findByRole('option', { name: /v1/ })
  expect(screen.getByLabelText('保存版本')).toHaveValue('v1')
  await userEvent.selectOptions(screen.getByLabelText('强化学习模型'), '')
  expect(screen.getByLabelText('保存版本')).toHaveValue('')
})
