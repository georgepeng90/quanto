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
  await userEvent.selectOptions(screen.getByLabelText('标的组合'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易策略'), 'etf-trading:v1')
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
  await userEvent.selectOptions(screen.getByLabelText('标的组合'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易策略'), 'etf-trading:v1')
  fireEvent.change(screen.getByLabelText('绩效起始日'), { target: { value: '2024-02-01' } })
  fireEvent.change(screen.getByLabelText('结束日'), { target: { value: '2024-03-01' } })
  fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '100000' } })
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: '探索' } })
  await userEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(api.createExperiment).toHaveBeenCalledTimes(1))
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled()
  expect((await screen.findAllByText(/该方案未接入/)).length).toBeGreaterThan(0)
})

test('editing after capability check starts discards its late ready response', async () => {
  let resolve!: (report: typeof ready) => void
  vi.spyOn(api, 'checkCapabilities').mockReturnValue(new Promise(done => { resolve = done }))
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments/new'] })
  render(<RouterProvider router={router} />)
  await userEvent.selectOptions(await screen.findByLabelText('选择策略'), 's1')
  await userEvent.selectOptions(screen.getByLabelText('选择数据'), 'd1')
  await userEvent.selectOptions(screen.getByLabelText('标的组合'), 'b1')
  await userEvent.selectOptions(screen.getByLabelText('交易策略'), 'etf-trading:v1')
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: '新的用途' } })
  await act(async () => resolve(ready))
  expect(screen.queryByLabelText('能力检查报告')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '提交回测' })).toBeDisabled()
})
