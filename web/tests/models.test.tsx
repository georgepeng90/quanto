import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'
import { ModelBacktestForm } from '../src/components/ModelBacktestForm'

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
  supported_instrument_types: string[];
  action_version?: string; reward_version?: string; allowed_trading_refs?: { kind: string; id: string; version: string }[] }
let definitionItems: TestDefinition[] = [
  { ref: learningRef, supported_instrument_types: ['etf'], action_version: 'hold-target-cash-quartiles-v2', reward_version: 'differential-sharpe-v1',
    allowed_trading_refs: [tradingRef], parameter_schema: { type: 'object', properties: { lookback: { type: 'integer', minimum: 3 },
      frequency: { type: 'string', enum: ['weekly', 'monthly'] }, action_version: { type: 'string', const: 'hold-target-cash-quartiles-v2' },
      reward_version: { type: 'string', const: 'differential-sharpe-v1' } } } },
  { ref: tradingRef, supported_instrument_types: ['etf'], parameter_schema: { type: 'object', properties: {} } },
]
const definitions = definitionItems
const refKey = (ref: typeof learningRef | typeof tradingRef) => JSON.stringify([ref.kind, ref.id, ref.version])
const posts: { path: string; body: any }[] = []
const backtestReport = { operation: 'policy_backtest', status: 'ready', request_hash: 'hash', dataset_version_id: 'd1', input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null }
let preview: (body: any) => Response | Promise<Response>
const adjustedPreview = () => Response.json({ requested_interval: { start: '2023-01-01', end: '2023-12-31' }, available_interval: { start: '2023-02-01', end: '2023-11-30', trading_sessions: 200 }, requires_confirmation: true, report: backtestReport, reasons: ['可用数据仅覆盖部分日期'] })
const app = (path: string) => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />)

beforeEach(() => {
  posts.length = 0
  preview = body => Response.json({ requested_interval: { start: body.spec.start, end: body.spec.end }, available_interval: { start: body.spec.start, end: body.spec.end, trading_sessions: 240 }, requires_confirmation: false, report: backtestReport, reasons: [] })
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
  definitionItems = definitions
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    if (init?.method === 'POST') {
      const body = JSON.parse(String(init.body)); posts.push({ path, body })
      if (path.endsWith('/backtests/preview')) return preview(body)
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

test('backtest requires a saved version and submits compatible entered dates without a purpose selection', async () => {
  app('/runs?model=m1')
  await screen.findByRole('option', { name: /第 1 轮/ })
  expect(screen.getByLabelText('训练轮次')).toHaveValue('')
  expect(screen.queryByLabelText('评估用途')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('已保存版本')
  expect(posts).toHaveLength(0)
  await userEvent.selectOptions(screen.getByLabelText('训练轮次'), 'v1')
  fireEvent.change(screen.getByLabelText('回测开始'), { target: { value: '2023-01-01' } })
  fireEvent.change(screen.getByLabelText('回测结束'), { target: { value: '2023-12-31' } })
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  await screen.findByText('模型回测已提交。')
  expect(posts.map(post => post.path)).toEqual(['/api/investing/models/m1/backtests/preview', '/api/investing/models/m1/backtests'])
  expect(posts.find(post => post.path.endsWith('/backtests'))?.body).toEqual({ dataset_id: 'd1', version_id: 'v1',
    spec: { start: '2023-01-01', end: '2023-12-31', baseline: 'policy', cost_multiplier: 1 } })
})

const enterBacktestDates = async () => {
  await screen.findByRole('option', { name: /第 1 轮/ })
  await userEvent.selectOptions(screen.getByLabelText('训练轮次'), 'v1')
  fireEvent.change(screen.getByLabelText('回测开始'), { target: { value: '2023-01-01' } })
  fireEvent.change(screen.getByLabelText('回测结束'), { target: { value: '2023-12-31' } })
}

test.each(['cancel button', 'Escape'])('adjusted dates require confirmation and %s preserves inputs without submitting', async action => {
  preview = adjustedPreview
  app('/runs?model=m1')
  await enterBacktestDates()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  const dialog = await screen.findByRole('dialog', { name: '确认回测区间' })
  expect(dialog).toHaveTextContent('2023-01-01—2023-12-31')
  expect(dialog).toHaveTextContent('2023-02-01—2023-11-30')
  expect(dialog).toHaveTextContent('可用数据仅覆盖部分日期')
  expect(posts.some(post => post.path.endsWith('/backtests'))).toBe(false)
  if (action === 'Escape') fireEvent(dialog, new Event('cancel', { cancelable: true }))
  else await userEvent.click(within(dialog).getByRole('button', { name: '取消' }))
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(screen.getByLabelText('回测开始')).toHaveValue('2023-01-01')
  expect(screen.getByLabelText('回测结束')).toHaveValue('2023-12-31')
  expect(posts.some(post => post.path.endsWith('/backtests'))).toBe(false)
  expect(screen.getByRole('button', { name: '开始回测' })).toHaveFocus()
})

test('confirmation updates inputs and submits exactly the suggested interval', async () => {
  preview = adjustedPreview
  app('/runs?model=m1')
  await enterBacktestDates()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  await userEvent.click(await screen.findByRole('button', { name: '确认并回测' }))
  await screen.findByText('模型回测已提交。')
  expect(screen.getByLabelText('回测开始')).toHaveValue('2023-02-01')
  expect(screen.getByLabelText('回测结束')).toHaveValue('2023-11-30')
  expect(posts.filter(post => post.path.endsWith('/backtests')).map(post => post.body)).toEqual([
    { dataset_id: 'd1', version_id: 'v1', spec: { start: '2023-02-01', end: '2023-11-30', baseline: 'policy', cost_multiplier: 1 } },
  ])
})

test.each([null, { start: '2023-02-01', end: '2023-11-30', trading_sessions: 200 }])('a blocked report prevents submission even with available interval %j', async availableInterval => {
  preview = () => Response.json({ requested_interval: { start: '2023-01-01', end: '2023-12-31' }, available_interval: availableInterval, requires_confirmation: !!availableInterval, report: { ...backtestReport, status: 'blocked' }, reasons: ['区间内没有连续可回测数据'] })
  app('/runs?model=m1')
  await enterBacktestDates()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('区间内没有连续可回测数据')
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(posts.some(post => post.path.endsWith('/backtests'))).toBe(false)
})

test('a validation failure from an unmounted model cannot overwrite the current form', async () => {
  let fail!: (value: Error) => void
  preview = () => new Promise<Response>((_, reject) => { fail = reject })
  const oldView = render(<ModelBacktestForm initialModel="m1" initialVersion="v1" onSubmitted={vi.fn()} />)
  await enterBacktestDates()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  oldView.unmount()
  render(<ModelBacktestForm initialModel="m1" initialVersion="v1" onSubmitted={vi.fn()} />)
  await screen.findByRole('option', { name: /第 1 轮/ })
  await act(async () => fail(new Error('旧模型检查失败')))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '开始回测' })).toBeEnabled()
  expect(posts.some(post => post.path.endsWith('/backtests'))).toBe(false)
})

test('pending validation disables repeated starts and ignores results after unmount', async () => {
  let release!: (value: Response) => void
  preview = () => new Promise<Response>(resolve => { release = resolve })
  const submitted = vi.fn()
  const view = render(<ModelBacktestForm initialModel="m1" initialVersion="v1" onSubmitted={submitted} />)
  await enterBacktestDates()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  expect(screen.getByRole('button', { name: '检查日期中…' })).toBeDisabled()
  expect(screen.getByLabelText('强化学习模型')).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '检查日期中…' }))
  expect(posts).toHaveLength(1)
  view.unmount()
  await act(async () => release(Response.json({ available_interval: { start: '2023-01-01', end: '2023-12-31', trading_sessions: 240 }, requires_confirmation: false, report: backtestReport, reasons: [] })))
  expect(posts.some(post => post.path.endsWith('/backtests'))).toBe(false)
  expect(submitted).not.toHaveBeenCalled()
})

test('submission stays disabled until completion and does not enqueue a second backtest', async () => {
  let release!: (value: Response) => void
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/backtests') && init?.method === 'POST') {
      posts.push({ path: String(input), body: JSON.parse(String(init.body)) })
      return new Promise<Response>(resolve => { release = resolve })
    }
    return prior(input, init)
  }))
  const submitted = vi.fn()
  render(<ModelBacktestForm initialModel="m1" initialVersion="v1" onSubmitted={submitted} />)
  await enterBacktestDates()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  expect(await screen.findByRole('button', { name: '提交中…' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '提交中…' }))
  expect(posts.filter(post => post.path.endsWith('/backtests'))).toHaveLength(1)
  expect(submitted).not.toHaveBeenCalled()
  await act(async () => release(Response.json({ id: 'job-2', state: 'queued' })))
  expect(screen.getByRole('status')).toHaveTextContent('模型回测已提交。')
  expect(submitted).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: '开始回测' })).toBeEnabled()
})

test('submission errors display the latest server blocking report', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/backtests') && init?.method === 'POST') return Response.json({ code: 'FRAMEWORK_CONFLICT', message: '日期数据已发生变化', details: { report: { ...backtestReport, status: 'blocked', checks: [{ name: 'data', status: 'blocked', field_path: 'spec.start', code: 'DATA_REQUIREMENT_UNSATISFIED', message: '数据已不可用' }] } } }, { status: 409 })
    return prior(input, init)
  }))
  app('/runs?model=m1')
  await enterBacktestDates()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('日期数据已发生变化')
  expect(within(screen.getByRole('region', { name: '能力检查报告' })).getByRole('listitem')).toHaveTextContent('数据已不可用')
  expect(screen.getByRole('button', { name: '开始回测' })).toBeEnabled()
})
