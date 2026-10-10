import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'
import { api } from '../src/api'
import { FixedBacktestForm } from '../src/components/FixedBacktestForm'

const member = { exchange: 'SSE', instrument_type: 'etf' as const, code: '512480' }
const basket = { basket_id: 'b1', name: 'ETF 篮子', current_revision: 1, created_at: '2026-10-11T00:00:00Z' }
const revision = { basket_id: 'b1', revision: 1, members: [member], created_at: basket.created_at, selection_note: '研究选择', content_hash: 'h' }
const detail = { ...basket, current: revision, revisions: [{ revision: 1, content_hash: 'h', created_at: basket.created_at }] }
const trading = { ref: { kind: 'trading' as const, id: 'etf-trading', version: 'v1' }, implementation_hash: 'h', definition_hash: 'h', supported_instrument_types: ['etf'], parameter_schema: {} }
const strategy = { id: 's1', content_hash: 'h', manifest: { name: '趋势', entrypoint: 'strategy.py:create_strategy', parameter_schema: { type: 'object', properties: { window: { type: 'integer', title: '观察窗口', default: 20 } }, required: ['window'] }, auxiliary_sources: [], research_data: [] } }
const dataset = { id: 'd1', content_hash: 'h', manifest: { source: 'ETF 日线', is_demo: false, price_mode: 'adjusted_research', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'complete', limitations: [] } }
const ready = { operation: 'fixed_backtest' as const, status: 'ready' as const, request_hash: 'h', dataset_version_id: 'd1', input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null }

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'strategies').mockResolvedValue({ items: [strategy] as any, next_cursor: null })
  vi.spyOn(api, 'strategy').mockResolvedValue(strategy as any)
  vi.spyOn(api, 'datasets').mockResolvedValue({ items: [dataset] as any, next_cursor: null })
  vi.spyOn(api, 'dataset').mockResolvedValue(dataset as any)
  vi.spyOn(api, 'experiments').mockResolvedValue({ items: [], next_cursor: null })
  vi.spyOn(api, 'listDefinitions').mockResolvedValue({ items: [trading] as any })
  vi.spyOn(api, 'listBaskets').mockResolvedValue({ items: [basket], next_cursor: null })
  vi.spyOn(api, 'getBasket').mockResolvedValue(detail)
  vi.spyOn(api, 'getBasketRevision').mockResolvedValue(revision)
  vi.spyOn(api, 'checkCapabilities').mockResolvedValue(ready)
  vi.spyOn(api, 'createExperiment').mockImplementation(async payload => ({ id: 'e1', revision: 1, payload }))
  vi.spyOn(api, 'submitRun').mockResolvedValue({ run_id: 'r1' } as any)
})

test('run form sends only ordered basket reference and strict fixed request', async () => {
  render(<MemoryRouter><FixedBacktestForm onSubmitted={vi.fn()} /></MemoryRouter>)
  await userEvent.selectOptions(await screen.findByLabelText('策略'), 's1')
  await userEvent.selectOptions(screen.getByLabelText('标的篮子'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), 'etf-trading:v1')
  fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '100000' } })
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  await waitFor(() => expect(api.createExperiment).toHaveBeenCalledTimes(1))
  const payload = vi.mocked(api.createExperiment).mock.calls[0][0]
  expect(payload).toMatchObject({ framework_contract_version: 'multi-asset-v1', basket_revision_ref: { basket_id: 'b1', revision: 1 }, trading_definition_ref: trading.ref, strategy_params: { window: 20 } })
  expect(payload.backtest_spec).not.toHaveProperty('symbols')
  expect(payload).not.toHaveProperty('effective_strategy_params')
})

test('incomplete fixed draft saves, while blocked service report prevents a run', async () => {
  vi.spyOn(api, 'checkCapabilities').mockResolvedValue({ ...ready, status: 'blocked', request_hash: null, checks: [{ name: 'definition', status: 'blocked', field_path: 'trading_definition_ref', code: 'DEFINITION_UNAVAILABLE', message: '该方案未接入' }] })
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/new'] })
  render(<RouterProvider router={router} />)
  await userEvent.selectOptions(await screen.findByLabelText('选择策略'), 's1')
  await userEvent.selectOptions(screen.getByLabelText('选择数据'), 'd1')
  await userEvent.selectOptions(screen.getByLabelText('标的篮子'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), 'etf-trading:v1')
  fireEvent.change(screen.getByLabelText('绩效起始日'), { target: { value: '2024-02-01' } })
  fireEvent.change(screen.getByLabelText('结束日'), { target: { value: '2024-03-01' } })
  fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '100000' } })
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: '探索' } })
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  await waitFor(() => expect(api.createExperiment).toHaveBeenCalledTimes(1))
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled()
  expect(await screen.findByText(/该方案未接入/)).toBeInTheDocument()
})

test('editing after capability check starts discards its late ready response', async () => {
  let resolve!: (report: typeof ready) => void
  vi.spyOn(api, 'checkCapabilities').mockReturnValue(new Promise(done => { resolve = done }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/new'] })
  render(<RouterProvider router={router} />)
  await userEvent.selectOptions(await screen.findByLabelText('选择策略'), 's1')
  await userEvent.selectOptions(screen.getByLabelText('选择数据'), 'd1')
  await userEvent.selectOptions(screen.getByLabelText('标的篮子'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), 'etf-trading:v1')
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: '新的用途' } })
  await act(async () => resolve(ready))
  expect(screen.queryByLabelText('能力检查报告')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled()
})

test('historical experiment is read only and offers an explicit framework draft', async () => {
  vi.spyOn(api, 'experiment').mockResolvedValue({ id: 'old', revision: 1, payload: { strategy_revision_id: 's1', dataset_version_id: 'd1', strategy_params: {}, backtest_spec: { start: '2024-02-01', end: '2024-03-01', symbols: ['512480'] }, sample_purpose: '探索', source_run_id: 'r-old' } } as any)
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/old'] })
  render(<RouterProvider router={router} />)
  expect(await screen.findByRole('heading', { name: '历史实验（只读）' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '保存实验' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: '创建新的框架草稿' })).toHaveAttribute('href', '/experiments/new?source_experiment_id=old')
})

test.each([
  { parentRun: undefined, currentWindow: 7 },
  { parentRun: 'r-parent', currentWindow: 11 },
])('upgrades the selected legacy revision with current parameters and parent run $parentRun', async ({ parentRun, currentWindow }) => {
  const legacy = { id: 'old', revision: 4, payload: {
    strategy_revision_id: 's1', dataset_version_id: 'd1', strategy_params: { window: currentWindow },
    backtest_spec: { start: '2024-02-01', end: '2024-03-01', symbols: ['512480'], initial_cash: 100000, seed: 2,
      execution: { model: 'next_open', commission_rate: 0, minimum_commission: 0, slippage_rate: 0, max_stale_sessions: 0 },
      risk: { max_total_weight: 1, max_symbol_weight: 1, group_caps: {}, stress_scenarios: [], pause_action: 'block_new', recovery_mode: 'never' },
      metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: '1' } },
    sample_purpose: '当前实验', ...(parentRun ? { source_run_id: parentRun } : {})
  } }
  vi.spyOn(api, 'experiment').mockResolvedValue(legacy as any)
  const readRun = vi.spyOn(api, 'run')
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/old'] })
  render(<RouterProvider router={router} />)
  await userEvent.click(await screen.findByRole('link', { name: '创建新的框架草稿' }))
  expect(await screen.findByRole('heading', { name: '新建固定策略' })).toBeInTheDocument()
  expect(screen.getByLabelText('样本用途')).toHaveValue('当前实验')
  expect(screen.getByLabelText('初始资金')).toHaveValue(100000)
  expect(await screen.findByLabelText('观察窗口')).toHaveValue(currentWindow)
  expect(screen.getByLabelText('标的篮子')).toHaveValue('')
  expect(screen.getByLabelText('交易定义')).toHaveValue('')
  await userEvent.selectOptions(screen.getByLabelText('标的篮子'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), 'etf-trading:v1')
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  await waitFor(() => expect(api.createExperiment).toHaveBeenCalledOnce())
  const payload = vi.mocked(api.createExperiment).mock.calls[0][0]
  expect(payload).toMatchObject({ source_experiment_ref: { experiment_id: 'old', revision: 4 }, strategy_revision_id: 's1', dataset_version_id: 'd1', strategy_params: { window: currentWindow }, sample_purpose: '当前实验' })
  expect(payload.source_run_id).toBe(parentRun)
  expect(payload.backtest_spec).not.toHaveProperty('symbols')
  expect(readRun).not.toHaveBeenCalled()
})

test('late legacy source read cannot overwrite later form edits', async () => {
  let resolve!: (value: any) => void
  vi.spyOn(api, 'experiment').mockReturnValue(new Promise(done => { resolve = done }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/new?source_experiment_id=old'] })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect(api.experiment).toHaveBeenCalledWith('old'))
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: '用户新输入' } })
  await act(async () => resolve({ id: 'old', revision: 2, payload: { sample_purpose: '旧值' } }))
  expect(screen.getByLabelText('样本用途')).toHaveValue('用户新输入')
  expect(screen.getByLabelText('标的篮子')).toHaveValue('')
})

test('legacy source failure after leaving its URL does not surface on the new page', async () => {
  let reject!: (reason: Error) => void
  vi.spyOn(api, 'experiment').mockReturnValue(new Promise((_done, fail) => { reject = fail }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/new?source_experiment_id=old'] })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect(api.experiment).toHaveBeenCalledWith('old'))
  await act(async () => { await router.navigate('/experiments/new') })
  await act(async () => reject(new Error('late source failure')))
  expect(screen.queryByText(/late source failure/)).not.toBeInTheDocument()
})

test('late run source read cannot overwrite later form edits', async () => {
  let resolve!: (value: any) => void
  vi.spyOn(api, 'run').mockReturnValue(new Promise(done => { resolve = done }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/new?source_run_id=r1'] })
  render(<RouterProvider router={router} />)
  await waitFor(() => expect(api.run).toHaveBeenCalledWith('r1'))
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: '用户新输入' } })
  await act(async () => resolve({ run_id: 'r1', provenance: { sample_purpose: '旧运行', backtest_spec: { start: '2024-02-01', end: '2024-03-01', symbols: ['512480'] } } }))
  expect(screen.getByLabelText('样本用途')).toHaveValue('用户新输入')
})
