import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, test, vi } from 'vitest'
import { FixedBacktestForm } from '../src/components/FixedBacktestForm'
import { api } from '../src/api'

const spec = { start: '2024-01-01', end: '2024-12-31', symbols: ['ETF1', 'ETF2'], initial_cash: 100000, seed: 42, execution: { model: 'next_open', commission_rate: .001, minimum_commission: 5, slippage_rate: .002, max_stale_sessions: 3 }, risk: { max_total_weight: .9, max_symbol_weight: .5, group_caps: {}, stress_scenarios: [], pause_action: 'block_new', recovery_mode: 'never' }, metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: '1' } }
const draft = { id: 'opaque-experiment', revision: 3, payload: { strategy_revision_id: 's1', dataset_version_id: 'd1', strategy_params: { window: 4 }, backtest_spec: spec, sample_purpose: '验证' } }
const strategy = { id: 's1', manifest: { name: '趋势轮动', entrypoint: 'strategy.py:create_strategy', parameter_schema: { type: 'object', properties: { window: { type: 'integer', title: '观察窗口', minimum: 1, default: 20 } }, required: ['window'] } } }
const dataset = { id: 'd1', manifest: { source: 'ETF日线', data_start: '2020-01-01', data_end: '2025-12-31' } }
const onSubmitted = vi.fn()
beforeEach(() => {
  vi.restoreAllMocks(); onSubmitted.mockReset()
  vi.spyOn(api, 'strategies').mockResolvedValue({ items: [strategy] as any, next_cursor: null })
  vi.spyOn(api, 'datasets').mockResolvedValue({ items: [dataset] as any, next_cursor: null })
  vi.spyOn(api, 'experiments').mockResolvedValue({ items: [draft] as any, next_cursor: null })
  vi.spyOn(api, 'createExperiment').mockImplementation(async payload => ({ id: `variant-${vi.mocked(api.createExperiment).mock.calls.length}`, revision: 1, payload }))
  vi.spyOn(api, 'submitRun').mockResolvedValue({ run_id: 'r1' } as any)
  vi.spyOn(api, 'submitBatch').mockResolvedValue({ items: [] })
})
async function open() {
  render(<FixedBacktestForm onSubmitted={onSubmitted} />)
  await screen.findAllByRole('option', { name: /趋势轮动/ })
  await userEvent.selectOptions(screen.getByLabelText('策略'), 's1')
}

test('selects a named strategy, edits dates and parameters, and saves the submitted configuration internally', async () => {
  await open()
  expect(screen.queryByLabelText('实验草稿')).toBeNull()
  expect(screen.queryByRole('textbox', { name: /JSON/ })).toBeNull()
  expect(screen.getByLabelText('回测开始')).toHaveValue('2024-01-01')
  fireEvent.change(screen.getByLabelText('回测开始'), { target: { value: '2024-06-01' } })
  fireEvent.change(screen.getByLabelText('观察窗口'), { target: { value: '8' } })
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledTimes(1))
  expect(api.createExperiment).toHaveBeenCalledWith({ ...draft.payload, strategy_params: { window: 8 }, backtest_spec: { ...spec, start: '2024-06-01' } })
  expect(api.submitRun).toHaveBeenCalledWith('variant-1', 1, expect.any(String))
  expect(onSubmitted).toHaveBeenCalledTimes(1)
})

test('adds, edits and removes parameter groups and submits a batch in visible order', async () => {
  await open()
  await userEvent.click(screen.getByRole('button', { name: '添加一组参数' }))
  const second = screen.getByRole('group', { name: '第2组参数' })
  fireEvent.change(within(second).getByLabelText('观察窗口'), { target: { value: '8' } })
  expect(screen.getAllByLabelText('观察窗口')[0].id).not.toBe(screen.getAllByLabelText('观察窗口')[1].id)
  await userEvent.click(screen.getByRole('button', { name: '添加一组参数' }))
  await userEvent.click(within(screen.getByRole('group', { name: '第3组参数' })).getByRole('button', { name: '删除这组' }))
  await userEvent.click(screen.getByRole('button', { name: '运行2组回测' }))
  await waitFor(() => expect(api.submitBatch).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.createExperiment).mock.calls.map(([payload]) => payload.strategy_params)).toEqual([{ window: 4 }, { window: 8 }])
  expect(api.submitBatch).toHaveBeenCalledWith([{ experiment_id: 'variant-1', expected_revision: 1 }, { experiment_id: 'variant-2', expected_revision: 1 }], expect.any(String))
})

test('validates every parameter group and dataset dates before saving anything', async () => {
  await open()
  await userEvent.click(screen.getByRole('button', { name: '添加一组参数' }))
  fireEvent.change(within(screen.getByRole('group', { name: '第2组参数' })).getByLabelText('观察窗口'), { target: { value: '0' } })
  await userEvent.click(screen.getByRole('button', { name: '运行2组回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('第2组')
  expect(api.createExperiment).not.toHaveBeenCalled()
  fireEvent.change(within(screen.getByRole('group', { name: '第2组参数' })).getByLabelText('观察窗口'), { target: { value: '8' } })
  fireEvent.change(screen.getByLabelText('回测结束'), { target: { value: '2026-01-01' } })
  await userEvent.click(screen.getByRole('button', { name: '运行2组回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('覆盖范围')
  expect(api.createExperiment).not.toHaveBeenCalled()
})

test('retries a failed submission with the same saved configuration and idempotency key', async () => {
  vi.mocked(api.submitRun).mockRejectedValueOnce(new Error('network timeout'))
  await open()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  await userEvent.click(await screen.findByRole('button', { name: '重试提交' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledTimes(2))
  expect(vi.mocked(api.submitRun).mock.calls[1]).toEqual(vi.mocked(api.submitRun).mock.calls[0])
  expect(api.createExperiment).toHaveBeenCalledTimes(1)
})

test('can run a registered strategy without any saved experiment', async () => {
  vi.mocked(api.experiments).mockResolvedValue({ items: [], next_cursor: null })
  await open()
  expect(screen.getByLabelText('观察窗口')).toHaveValue(20)
  fireEvent.change(screen.getByLabelText('证券代码'), { target: { value: 'ETF1，ETF2' } })
  fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '100000' } })
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledTimes(1))
  expect(vi.mocked(api.createExperiment).mock.calls[0][0]).toMatchObject({ strategy_revision_id: 's1', dataset_version_id: 'd1', strategy_params: { window: 20 }, backtest_spec: { start: '2020-01-01', end: '2025-12-31', symbols: ['ETF1', 'ETF2'] } })
})

test('batch retry resumes after a saved group and preserves the batch key after transport failure', async () => {
  vi.mocked(api.createExperiment).mockResolvedValueOnce({ id: 'first', revision: 2 } as any).mockRejectedValueOnce(new Error('save failed')).mockResolvedValueOnce({ id: 'second', revision: 3 } as any)
  vi.mocked(api.submitBatch).mockRejectedValueOnce(new Error('network timeout'))
  await open()
  await userEvent.click(screen.getByRole('button', { name: '添加一组参数' }))
  await userEvent.click(screen.getByRole('button', { name: '运行2组回测' }))
  await userEvent.click(await screen.findByRole('button', { name: '重试提交' }))
  await waitFor(() => expect(api.submitBatch).toHaveBeenCalledTimes(1))
  await userEvent.click(screen.getByRole('button', { name: '重试提交' }))
  await waitFor(() => expect(api.submitBatch).toHaveBeenCalledTimes(2))
  expect(api.createExperiment).toHaveBeenCalledTimes(3)
  expect(vi.mocked(api.submitBatch).mock.calls[0]).toEqual(vi.mocked(api.submitBatch).mock.calls[1])
  expect(vi.mocked(api.submitBatch).mock.calls[0][0]).toEqual([{ experiment_id: 'first', expected_revision: 2 }, { experiment_id: 'second', expected_revision: 3 }])
})

test('locks editable fields during submission and discards prepared retries after an edit', async () => {
  let finish: ((value: any) => void) | undefined
  vi.mocked(api.createExperiment).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  vi.mocked(api.submitRun).mockRejectedValueOnce(new Error('network timeout'))
  await open()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  expect(screen.getByLabelText('策略')).toBeDisabled()
  expect(screen.getByLabelText('观察窗口')).toBeDisabled()
  finish!({ id: 'old-variant', revision: 1 })
  await screen.findByRole('button', { name: '重试提交' })
  fireEvent.change(screen.getByLabelText('观察窗口'), { target: { value: '8' } })
  expect(screen.queryByRole('button', { name: '重试提交' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '开始回测' }))
  await waitFor(() => expect(api.submitRun).toHaveBeenCalledTimes(2))
  expect(vi.mocked(api.createExperiment).mock.calls[1][0].strategy_params).toEqual({ window: 8 })
  expect(vi.mocked(api.submitRun).mock.calls[1][2]).not.toBe(vi.mocked(api.submitRun).mock.calls[0][2])
})

test('loads paginated choices and resets parameters when selecting another strategy', async () => {
  vi.mocked(api.strategies).mockResolvedValueOnce({ items: [strategy] as any, next_cursor: 'next' }).mockResolvedValueOnce({ items: [{ ...strategy, id: 's2', manifest: { ...strategy.manifest, name: '动量策略' } }] as any, next_cursor: null })
  await open()
  fireEvent.change(screen.getByLabelText('观察窗口'), { target: { value: '8' } })
  await userEvent.click(screen.getByRole('button', { name: '添加一组参数' }))
  await userEvent.selectOptions(screen.getByLabelText('策略'), 's2')
  expect(screen.getAllByLabelText('观察窗口')).toHaveLength(1)
  expect(screen.getByLabelText('观察窗口')).toHaveValue(20)
  expect(api.strategies).toHaveBeenCalledWith('next')
})

test('uses the most recent saved configuration and distinguishes same-name revisions without IDs', async () => {
  vi.mocked(api.experiments).mockResolvedValue({ items: [draft, { ...draft, id: 'new-config', payload: { ...draft.payload, strategy_params: { window: 8 } } }] as any, next_cursor: null })
  vi.mocked(api.strategies).mockResolvedValue({ items: [strategy, { ...strategy, id: 's2' }] as any, next_cursor: null })
  await open()
  expect(screen.getByLabelText('观察窗口')).toHaveValue(8)
  expect(screen.getByRole('option', { name: '趋势轮动 · 版本2' })).toHaveValue('s2')
  expect(screen.getByRole('option', { name: '趋势轮动 · 版本1' })).toHaveValue('s1')
})
