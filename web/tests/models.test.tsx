import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'

const asset = { exchange: 'SZSE', instrument_type: 'etf', code: '000001' }
const pair = { instrument_id: asset, local_symbol: 'LOCAL1', evidence_ref: 'e1' }
const binding = { model_id: 'm1', origin: 'new', binding_hash: 'bhash', original_spec_hash: 'shash',
  basket_revision_ref: { basket_id: 'b1', revision: 1 }, ordered_pairs: [pair], definitions: [], evidence: { kind: 'new' } }
const spec = { name: '研究模型', frequency: 'weekly', reward_version: 'differential-sharpe-v1', backtest: { symbols: ['LOCAL1'] },
  train_start: '2021-01-01', train_end: '2022-12-31', validation_start: '2023-01-01', validation_end: '2023-12-31',
  test_start: '2024-01-01', test_end: '2024-12-31', total_timesteps: 4096, device: 'cpu', selection_metric: 'validation_sharpe' }
const model = { id: 'm1', name: '研究模型', dataset_id: 'd1', spec, state: 'ready', active_job_id: null, total_timesteps: 4096,
  session_count: 1, version_count: 1, latest_version_id: 'v1', created_at: '2024-01-01T00:00:00Z', framework_binding: binding,
  framework_binding_check: null, active_session: null, capabilities: { cuda_available: false } }
const dataset = { id: 'd1', manifest: { data_start: '2020-01-01', data_end: '2025-12-31', price_mode: 'raw_cash', limitations: [] } }
const tradingRef = { kind: 'trading', id: 'etf-daily', version: '1' }
const learningRef = { kind: 'learning', id: 'etf-ppo-cash-dsr', version: '1' }
type TestDefinition = { ref: { kind: string; id: string; version: string }; parameter_schema: { type: string; properties: Record<string, unknown> };
  action_version?: string; reward_version?: string; allowed_trading_refs?: { kind: string; id: string; version: string }[] }
let definitionItems: TestDefinition[] = [
  { ref: learningRef, action_version: 'hold-target-cash-quartiles-v2', reward_version: 'differential-sharpe-v1',
    allowed_trading_refs: [tradingRef], parameter_schema: { type: 'object', properties: { lookback: { type: 'integer', minimum: 3 },
      frequency: { type: 'string', enum: ['weekly', 'monthly'] }, action_version: { type: 'string', const: 'hold-target-cash-quartiles-v2' },
      reward_version: { type: 'string', const: 'differential-sharpe-v1' } } } },
  { ref: tradingRef, parameter_schema: { type: 'object', properties: {} } },
]
const definitions = definitionItems
const refKey = (ref: typeof learningRef | typeof tradingRef) => JSON.stringify([ref.kind, ref.id, ref.version])
const posts: { path: string; body: any }[] = []
const app = (path: string) => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />)

beforeEach(() => {
  posts.length = 0
  definitionItems = definitions
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); posts.push({ path, body })
      if (path === '/api/capabilities/check') return Response.json({ operation: body.operation, status: 'ready', request_hash: 'hash',
        dataset_version_id: 'd1', input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null })
      return Response.json(path === '/api/investing/models' ? { ...model, id: 'm2' } : { id: 'job-2', state: 'queued' })
    }
    if (path === '/api/definitions') return Response.json({ items: definitionItems })
    if (path === '/api/baskets') return Response.json({ items: [{ basket_id: 'b1', name: 'ETF basket', current_revision: 1, created_at: '' }], next_cursor: null })
    if (path === '/api/baskets/b1') return Response.json({ basket_id: 'b1', name: 'ETF basket', current_revision: 1, created_at: '',
      current: { ...binding.basket_revision_ref, members: [asset], selection_note: 'research', content_hash: 'hash', created_at: '' },
      revisions: [{ revision: 1, content_hash: 'hash', created_at: '' }] })
    if (path === '/api/baskets/b1/revisions/1') return Response.json({ ...binding.basket_revision_ref, members: [asset], selection_note: 'research', content_hash: 'hash', created_at: '' })
    if (path === '/api/investing/models/m1?include_history=false') return Response.json(model)
    if (path === '/api/investing/models/m1') return Response.json(model)
    if (path.startsWith('/api/investing/models/m1/history?')) return Response.json({ items: [{ id: 'v1', round_number: 1,
      session_number: 1, state: 'succeeded', selection_valid: true, best_source_version_id: 'v1', timesteps_completed: 4096 }],
      total: 1, page: 1, page_size: 50, total_pages: 1, anchor: 1 })
    if (path === '/api/investing/models') return Response.json({ items: [model], next_cursor: null })
    if (path === '/api/investing/overview') return Response.json({ datasets: [dataset], policies: [], backtests: [], jobs: [], capabilities: { cuda_available: false } })
    if (path === '/api/backtests/catalog?limit=50' || path === '/api/experiments') return Response.json({ items: [], next_cursor: null })
    throw new Error(`Unexpected ${path}`)
  }))
})

test('creates a model from an ordered basket and selected installed definitions', async () => {
  app('/models')
  await userEvent.click(await screen.findByRole('button', { name: '新建模型' }))
  await userEvent.selectOptions(await screen.findByLabelText('日线数据版本'), 'd1')
  await userEvent.selectOptions(await screen.findByLabelText('标的组合'), 'b1')
  await userEvent.selectOptions(await screen.findByLabelText('学习定义'), refKey(learningRef))
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), refKey(tradingRef))
  fireEvent.change(screen.getByLabelText('lookback'), { target: { value: '30' } })
  await userEvent.selectOptions(screen.getByLabelText('frequency'), '0')
  fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: '新模型' } })
  for (const [label, value] of [['训练开始', '2021-01-01'], ['训练结束', '2022-12-31'], ['验证开始', '2023-01-01'], ['验证结束', '2023-12-31']])
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  expect(screen.getByRole('button', { name: '创建模型' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '创建模型' })).toBeEnabled())
  await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
  const created = posts.find(post => post.path === '/api/investing/models')!.body
  expect(created).toMatchObject({ framework_contract_version: 'multi-asset-v1', basket_revision_ref: { basket_id: 'b1', revision: 1 },
    learning_definition_ref: learningRef, trading_definition_ref: tradingRef,
    spec: { action_version: 'hold-target-cash-quartiles-v2', reward_version: 'differential-sharpe-v1', lookback: 30,
      frequency: 'weekly', backtest: { start: '2021-01-01', end: '2023-12-31' } } })
  expect(created.spec.backtest).not.toHaveProperty('symbols')
  expect(posts.some(post => post.path.endsWith('/train'))).toBe(false)
})

test('distinguishes learning and trading definitions with the same id across versions', async () => {
  const laterLearning = { ...learningRef, version: '2' }
  const laterTrading = { ...tradingRef, version: '2' }
  definitionItems = [
    definitions[0], definitions[1],
    { ...definitions[0], ref: laterLearning, allowed_trading_refs: [laterTrading] },
    { ...definitions[1], ref: laterTrading },
  ]
  app('/models')
  await userEvent.click(await screen.findByRole('button', { name: '新建模型' }))
  await userEvent.selectOptions(await screen.findByLabelText('日线数据版本'), 'd1')
  await userEvent.selectOptions(await screen.findByLabelText('标的组合'), 'b1')
  await userEvent.selectOptions(await screen.findByLabelText('学习定义'), refKey(laterLearning))
  expect(screen.getByRole('combobox', { name: '交易定义' }).querySelectorAll('option')).toHaveLength(2)
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), refKey(laterTrading))
  fireEvent.change(screen.getByLabelText('lookback'), { target: { value: '30' } })
  await userEvent.selectOptions(screen.getByLabelText('frequency'), '0')
  fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: '版本二模型' } })
  for (const [label, value] of [['训练开始', '2021-01-01'], ['训练结束', '2022-12-31'], ['验证开始', '2023-01-01'], ['验证结束', '2023-12-31']])
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '创建模型' })).toBeEnabled())
  await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
  expect(posts.find(post => post.path === '/api/investing/models')?.body).toMatchObject({
    learning_definition_ref: laterLearning, trading_definition_ref: laterTrading,
  })
})

test('includes additional learning schema fields in the capability request', async () => {
  definitionItems = [{ ...definitions[0], parameter_schema: { type: 'object', properties: {
    ...definitions[0].parameter_schema.properties, experimental_window: { type: 'integer', minimum: 1 },
  } } }, definitions[1]]
  app('/models')
  await userEvent.click(await screen.findByRole('button', { name: '新建模型' }))
  await userEvent.selectOptions(await screen.findByLabelText('日线数据版本'), 'd1')
  await userEvent.selectOptions(await screen.findByLabelText('标的组合'), 'b1')
  await userEvent.selectOptions(await screen.findByLabelText('学习定义'), refKey(learningRef))
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), refKey(tradingRef))
  fireEvent.change(screen.getByLabelText('lookback'), { target: { value: '30' } })
  await userEvent.selectOptions(screen.getByLabelText('frequency'), '0')
  fireEvent.change(screen.getByLabelText('experimental_window'), { target: { value: '12' } })
  fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: '扩展模型' } })
  for (const [label, value] of [['训练开始', '2021-01-01'], ['训练结束', '2022-12-31'], ['验证开始', '2023-01-01'], ['验证结束', '2023-12-31']])
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  await waitFor(() => expect(posts.some(post => post.path === '/api/capabilities/check' && post.body.operation === 'train')).toBe(true))
  expect(posts.find(post => post.path === '/api/capabilities/check' && post.body.operation === 'train')?.body.spec.experimental_window).toBe(12)
})

test('model detail shows binding evidence and gates training on a ready capability report', async () => {
  app('/models/m1')
  expect(await screen.findByText(/SZSE:000001/)).toBeVisible()
  expect(screen.getByRole('button', { name: '继续训练' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('追加决策步数'), { target: { value: '8192' } })
  await userEvent.click(screen.getByRole('button', { name: '检查续训能力' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '继续训练' })).toBeEnabled())
  await userEvent.click(screen.getByRole('button', { name: '继续训练' }))
  expect(posts.find(post => post.path.endsWith('/train'))?.body).toEqual({ additional_timesteps: 8192, device: 'cpu' })
})

test('backtest requires an explicit saved version and evaluation purpose', async () => {
  app('/runs?model=m1')
  await screen.findByRole('option', { name: /第 1 轮/ })
  expect(screen.getByLabelText('训练轮次')).toHaveValue('')
  await userEvent.selectOptions(screen.getByLabelText('训练轮次'), 'v1')
  fireEvent.change(screen.getByLabelText('回测开始'), { target: { value: '2023-01-01' } })
  fireEvent.change(screen.getByLabelText('回测结束'), { target: { value: '2023-12-31' } })
  await userEvent.selectOptions(screen.getByLabelText('评估用途'), 'development_replay')
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '提交模型回测' })).toBeEnabled())
  await userEvent.selectOptions(screen.getByLabelText('评估用途'), 'post_selection_backtest')
  expect(screen.getByRole('button', { name: '提交模型回测' })).toBeDisabled()
  await userEvent.selectOptions(screen.getByLabelText('评估用途'), 'development_replay')
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '提交模型回测' })).toBeEnabled())
  await userEvent.click(screen.getByRole('button', { name: '提交模型回测' }))
  expect(posts.find(post => post.path.endsWith('/backtests'))?.body).toEqual({ dataset_id: 'd1', version_id: 'v1',
    spec: { start: '2023-01-01', end: '2023-12-31', baseline: 'policy', cost_multiplier: 1,
      evaluation_purpose: 'development_replay' } })
})
