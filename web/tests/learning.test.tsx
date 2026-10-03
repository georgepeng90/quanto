import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { LearningPage } from '../src/pages/LearningPage'
import { api } from '../src/api'

vi.mock('../src/api', () => ({ api: { learningOverview: vi.fn(), prepareLearning: vi.fn(), trainLearning: vi.fn(), backtestLearning: vi.fn(), learningArtifact: vi.fn(), learningChart: vi.fn(), learningTable: vi.fn(), exportLearning: vi.fn(), jobLogs: vi.fn(), cancelJob: vi.fn() }, describeError: (error: Error) => error.message }))
const dataset = { id: 'd1', kind: 'dataset', manifest: { name: '分钟训练集', row_count: 1000, frequency_minutes: 5, instruments: ['ETF'], query: { frequency: '1m' }, limitations: ['合成数据'] } }
const model = { id: 'm1', kind: 'model', job_id: 'train1', manifest: { config: { name: 'GRU 模型', model_type: 'gru' }, cutoff: '2024-06-01T00:00:00Z', metrics: { test_loss: .001 } } }
const result = { id: 'b1', kind: 'backtest', manifest: { metrics: { total_return: .1, sharpe: 1.2, max_drawdown: -.05, minute_max_drawdown: -.08 }, assumptions: { execution: '下一根开盘模拟' }, counts: { fills: 20 }, period: { start: '2024-07-01T01:30:00Z', end: '2024-08-01T07:00:00Z' } } }
const overview = { capabilities: { torch_available: true, cuda_available: false, device_name: 'CPU', torch_version: '2.8', python_version: '3.13', disk_free_bytes: 1024 ** 3 }, snapshots: [{ id: 's1', frequency: '1m', instruments: [{ exchange: 'SHSE', instrument_type: 'etf', code: '510300' }], start: '2024-01-01T01:30:00Z', end: '2024-12-31T07:00:00Z', price_basis: { mode: 'vendor_research', basis_id: 'fixture' }, query: { frequency: '1m', instruments: [{ exchange: 'SHSE', instrument_type: 'etf', code: '510300' }], start: '2024-01-01T01:30:00Z', end: '2024-12-31T07:00:00Z' }, limitations: ['合成数据'] }], datasets: [dataset], models: [model], backtests: [result], jobs: [] }
const app = (path = '/learning') => render(<MemoryRouter initialEntries={[path]}><LearningPage /></MemoryRouter>)

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.learningOverview).mockResolvedValue(overview as never)
  vi.mocked(api.prepareLearning).mockResolvedValue({ id: 'p1', kind: 'prepare_learning', state: 'queued' } as never)
  vi.mocked(api.trainLearning).mockResolvedValue({ id: 't1', kind: 'train', state: 'queued' } as never)
  vi.mocked(api.backtestLearning).mockResolvedValue({ id: 'b2', kind: 'model_backtest', state: 'queued' } as never)
  vi.mocked(api.learningArtifact).mockResolvedValue(result as never)
  vi.mocked(api.learningChart).mockResolvedValue({ points: [{ time: '2024-07-01T01:30:00Z', equity: 100, drawdown: 0 }, { time: '2024-07-02T01:30:00Z', equity: 110, drawdown: -.01 }], total_count: 2000 })
  vi.mocked(api.learningTable).mockResolvedValue({ items: [{ order_id: 'o1', quantity: 100 }], next_cursor: 100 })
  vi.mocked(api.jobLogs).mockResolvedValue({ items: [{ seq: 1, message: '训练轮次 1', phase: 'train' }], next_cursor: null } as never)
})

test('prepares the selected immutable snapshot and exposes hardware/limitations', async () => {
  app()
  expect(await screen.findByText(/CPU · PyTorch/)).toBeInTheDocument()
  expect(screen.getByLabelText('分钟快照')).toHaveValue('s1')
  await userEvent.click(screen.getByRole('button', { name: '准备训练集' }))
  expect(api.prepareLearning).toHaveBeenCalledWith({ spec: { name: '分钟训练集', snapshot_id: 's1', query: overview.snapshots[0].query, frequency_minutes: 5 } })
  expect(await screen.findByText('p1')).toBeInTheDocument()
})

test('training validates chronological splits and submits UTC from Shanghai inputs', async () => {
  app()
  await screen.findByText(/CPU · PyTorch/)
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('时间区间')
  const values = ['2024-01-01T09:30', '2024-03-01T15:00', '2024-03-01T15:00', '2024-04-01T15:00', '2024-04-01T15:00', '2024-05-01T15:00']
  const labels = ['训练开始', '训练结束', '验证开始', '验证结束', '测试开始', '测试结束']
  labels.forEach((label, index) => fireEvent.change(screen.getByLabelText(`${label}（上海时间）`), { target: { value: values[index] } }))
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  await waitFor(() => expect(api.trainLearning).toHaveBeenCalled())
  const payload = vi.mocked(api.trainLearning).mock.calls[0][0]
  expect(payload.dataset_id).toBe('d1')
  expect(payload.spec.train_start).toBe('2024-01-01T01:30:00.000Z')
  expect(payload.spec.test_end).toBe('2024-05-01T07:00:00.000Z')
})

test('backtest selects model, rejects overlap by default, and supports a baseline', async () => {
  app()
  await screen.findByText(/CPU · PyTorch/)
  fireEvent.change(screen.getByLabelText('回测开始（上海时间）'), { target: { value: '2024-07-01T09:30' } })
  fireEvent.change(screen.getByLabelText('回测结束（上海时间）'), { target: { value: '2024-08-01T15:00' } })
  expect(screen.getByLabelText('回测模型')).toHaveValue('m1')
  await userEvent.click(screen.getByRole('button', { name: '提交回测' }))
  expect(api.backtestLearning).toHaveBeenCalledWith(expect.objectContaining({ dataset_id: 'd1', model_id: 'm1', allow_in_sample: false }))
  fireEvent.change(screen.getByLabelText('回测方法'), { target: { value: 'cash' } })
  await userEvent.click(screen.getByRole('button', { name: '提交回测' }))
  expect(vi.mocked(api.backtestLearning).mock.calls[1][0]).not.toHaveProperty('model_id')
})

test('job log and cancellation are usable for active jobs', async () => {
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, jobs: [{ id: 't1', kind: 'train', state: 'running', error: null }] } as never)
  vi.mocked(api.cancelJob).mockResolvedValue({ id: 't1', kind: 'train', state: 'cancel_requested' } as never)
  app()
  await userEvent.click(await screen.findByRole('button', { name: '查看日志 t1' }))
  expect(await screen.findByText('训练轮次 1')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '取消 t1' }))
  expect(api.cancelJob).toHaveBeenCalledWith('t1')
  expect(await screen.findByText('取消中')).toBeInTheDocument()
})

test('presents daily/minute metrics, bounded curve, assumptions and paginated orders', async () => {
  app('/learning?backtest=b1')
  const resultPanel = screen.getByRole('region', { name: '回测结果' })
  expect(await within(resultPanel).findByText('10.00%')).toBeInTheDocument()
  expect(within(resultPanel).getByText('盘中最大回撤')).toBeInTheDocument()
  expect(screen.getByText('下一根开盘模拟')).toBeInTheDocument()
  expect(await screen.findByRole('img', { name: '分钟净值曲线' })).toBeInTheDocument()
  expect(api.learningChart).toHaveBeenCalledWith('b1', expect.any(AbortSignal))
  fireEvent.change(screen.getByLabelText('结果明细'), { target: { value: 'orders' } })
  expect(await screen.findByText('o1')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '下一页明细' }))
  await waitFor(() => expect(api.learningTable).toHaveBeenLastCalledWith('b1', 'orders', 100, expect.any(AbortSignal)))
})

test('empty snapshots explain importing real minute data', async () => {
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, snapshots: [], datasets: [], models: [], backtests: [] } as never)
  app()
  expect(await screen.findByText(/CSV\/Parquet/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '准备训练集' })).toBeDisabled()
})

test('invalid numeric controls are rejected before calling the server', async () => {
  app()
  await screen.findByText(/CPU · PyTorch/)
  fireEvent.change(screen.getByLabelText('回测开始（上海时间）'), { target: { value: '2024-07-01T09:30' } })
  fireEvent.change(screen.getByLabelText('回测结束（上海时间）'), { target: { value: '2024-08-01T15:00' } })
  fireEvent.change(screen.getByLabelText('总仓位上限'), { target: { value: '1.5' } })
  await userEvent.click(screen.getByRole('button', { name: '提交回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('总仓位上限')
  expect(api.backtestLearning).not.toHaveBeenCalled()
})

test('restores terminal checkpoint config and clones model config using registry dataset IDs', async () => {
  const spec = { name: '原始训练', train_start: '2024-01-01T01:30:00Z', train_end: '2024-02-01T07:00:00Z', validation_start: '2024-02-01T07:00:00Z', validation_end: '2024-03-01T07:00:00Z', test_start: '2024-03-01T07:00:00Z', test_end: '2024-04-01T07:00:00Z' }
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, datasets: [{ ...dataset, manifest: { ...dataset.manifest, id: 'content-hash' } }], jobs: [{ id: 'old-train', kind: 'train', state: 'interrupted', checkpoint_available: true, request: { dataset_id: 'd1', spec } }] } as never)
  vi.mocked(api.learningArtifact).mockResolvedValue({ ...model, manifest: { ...model.manifest, config: spec, dataset_id: 'content-hash' } } as never)
  app()
  await userEvent.click(await screen.findByRole('button', { name: '恢复配置 old-train' }))
  expect(screen.getByLabelText('恢复检查点任务 ID（可选）')).toHaveValue('old-train')
  expect(screen.getByLabelText('训练开始（上海时间）')).toHaveValue('2024-01-01T09:30')
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  expect(api.trainLearning).toHaveBeenCalledWith(expect.objectContaining({ resume_job_id: 'old-train', dataset_id: 'd1' }))
  await userEvent.click(screen.getByRole('button', { name: '克隆配置 m1' }))
  await waitFor(() => expect(screen.getByLabelText('恢复检查点任务 ID（可选）')).toHaveValue(''))
  expect(screen.getByLabelText('训练 / 回测数据集')).toHaveValue('d1')
})

test('surface server errors and abort overview requests on unmount', async () => {
  vi.mocked(api.prepareLearning).mockRejectedValue(new Error('磁盘空间不足'))
  const view = app()
  await userEvent.click(await screen.findByRole('button', { name: '准备训练集' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('磁盘空间不足')
  const signal = vi.mocked(api.learningOverview).mock.calls[0][0]!
  view.unmount()
  expect(signal.aborted).toBe(true)
})

test('shows nested model prediction metrics and structured financial assumptions', async () => {
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, models: [{ ...model, manifest: { ...model.manifest, metrics: { test: { mse: .001, mae: .02, samples: 120 } } } }] } as never)
  vi.mocked(api.learningArtifact).mockResolvedValue({ ...result, manifest: { ...result.manifest, assumptions: { execution: 'next available real bar open', limitations: ['unknown suspensions'] } } } as never)
  app('/learning?backtest=b1')
  expect(await screen.findByText('测试均方误差')).toBeInTheDocument()
  expect(screen.getByText('0.001')).toBeInTheDocument()
  expect(await screen.findByText('next available real bar open')).toBeInTheDocument()
  expect(screen.getByText('unknown suspensions')).toBeInTheDocument()
})

test('serial overview polling waits for responses and stops when unmounted', async () => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  let finish!: (value: never) => void
  vi.mocked(api.learningOverview).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const view = app()
  try {
    await act(async () => { await vi.advanceTimersByTimeAsync(12000) })
    expect(api.learningOverview).toHaveBeenCalledTimes(1)
    await act(async () => { finish(overview as never); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(api.learningOverview).toHaveBeenCalledTimes(2)
    view.unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(12000) })
    expect(api.learningOverview).toHaveBeenCalledTimes(2)
  } finally { view.unmount(); vi.useRealTimers() }
})

test('real structured instruments and documented import commands are readable', async () => {
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, snapshots: [{ ...overview.snapshots[0], instruments: [{ exchange: 'SHSE', instrument_type: 'etf', code: '510300' }] }] } as never)
  const view = app()
  expect(await screen.findByRole('option', { name: /SHSE:etf:510300/ })).toBeInTheDocument()
  expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument()
  view.unmount()
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, snapshots: [] } as never)
  app()
  expect(await screen.findByText(/scripts.import_market_data/)).toBeInTheDocument()
  expect(screen.getByText(/scripts.prefetch_market_data/)).toBeInTheDocument()
})

test('result tables display Shanghai timestamps and retain metrics if curve loading fails', async () => {
  vi.mocked(api.learningChart).mockRejectedValue(new Error('曲线校验失败'))
  vi.mocked(api.learningTable).mockResolvedValue({ items: [{ timestamp: '2024-07-01T01:30:00+00:00', signal_time: '2024-07-01T01:25:00Z' }], next_cursor: null })
  app('/learning?backtest=b1')
  const panel = screen.getByRole('region', { name: '回测结果' })
  expect(await within(panel).findByText('10.00%')).toBeInTheDocument()
  expect(within(panel).getByRole('alert')).toHaveTextContent('曲线校验失败')
  expect(await within(panel).findByText('2024-07-01 09:30:00')).toBeInTheDocument()
  expect(within(panel).getByText('2024-07-01 09:25:00')).toBeInTheDocument()
})

test('model details expose history and stop loading after an error', async () => {
  vi.mocked(api.learningArtifact).mockResolvedValueOnce({ ...model, manifest: { ...model.manifest, history: [{ epoch: 1, train_mse: 2e-8, validation_mse: .009 }] } } as never)
  const view = app()
  await userEvent.click(await screen.findByRole('button', { name: '查看模型 m1' }))
  expect(await screen.findByText('2.0000e-8')).toBeInTheDocument()
  expect(screen.getByText('0.009')).toBeInTheDocument()
  view.unmount()
  vi.mocked(api.learningArtifact).mockRejectedValue(new Error('模型读取失败'))
  app()
  await userEvent.click(await screen.findByRole('button', { name: '查看模型 m1' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('模型读取失败')
  expect(screen.queryByText('加载模型详情…')).not.toBeInTheDocument()
})

test('model cutoff blocks earlier playback unless research replay is explicit', async () => {
  app()
  await screen.findByText(/CPU · PyTorch/)
  fireEvent.change(screen.getByLabelText('回测开始（上海时间）'), { target: { value: '2024-05-01T09:30' } })
  fireEvent.change(screen.getByLabelText('回测结束（上海时间）'), { target: { value: '2024-08-01T15:00' } })
  await userEvent.click(screen.getByRole('button', { name: '提交回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('训练/验证截止时间')
  expect(api.backtestLearning).not.toHaveBeenCalled()
  await userEvent.click(screen.getByLabelText('允许样本内研究（非独立留出证据）'))
  await userEvent.click(screen.getByRole('button', { name: '提交回测' }))
  expect(api.backtestLearning).toHaveBeenCalledWith(expect.objectContaining({ allow_in_sample: true }))
})

test('resume preserves second and millisecond precision in public split config', async () => {
  const spec = { name: '精确区间', train_start: '2024-01-01T01:30:17.123Z', train_end: '2024-02-01T07:00:17.123Z', validation_start: '2024-02-01T07:00:17.123Z', validation_end: '2024-03-01T07:00:17.123Z', test_start: '2024-03-01T07:00:17.123Z', test_end: '2024-04-01T07:00:17.123Z' }
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, jobs: [{ id: 'precise', kind: 'train', state: 'failed', checkpoint_available: true, request: { dataset_id: 'd1', spec } }] } as never)
  app()
  await userEvent.click(await screen.findByRole('button', { name: '恢复配置 precise' }))
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  expect(api.trainLearning).toHaveBeenCalledWith(expect.objectContaining({ spec: expect.objectContaining({ train_start: '2024-01-01T01:30:17.123Z' }) }))
})

test('backtest results open by URL while the combined catalog remains the result list', async () => {
  app('/learning?backtest=b1')
  const panel = await screen.findByRole('region', { name: '回测结果' })
  expect(panel.compareDocumentPosition(screen.getByRole('heading', { name: '1 · 准备训练集' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getByRole('link', { name: '全部回测' })).toHaveAttribute('href', '/runs')
  expect(screen.queryByRole('button', { name: '查看结果 b1' })).toBeNull()
  expect(api.learningArtifact).toHaveBeenCalledWith('backtests', 'b1', expect.any(AbortSignal))
})

test('saved in-sample research remains visible in the selected result independently of the form', async () => {
  const research = { ...result, manifest: { ...result.manifest, metadata: { allow_in_sample: true } } }
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, backtests: [research, { ...result, id: 'b2' }] } as never)
  vi.mocked(api.learningArtifact).mockResolvedValue(research as never)
  app('/learning?backtest=b1')
  expect(screen.getByLabelText('允许样本内研究（非独立留出证据）')).not.toBeChecked()
  const panel = screen.getByRole('region', { name: '回测结果' })
  expect(await within(panel).findByText('样本内研究 · 非独立留出证据')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '刷新' }))
  await waitFor(() => expect(api.learningOverview).toHaveBeenCalledTimes(2))
  expect(within(panel).getByText('样本内研究 · 非独立留出证据')).toBeInTheDocument()
})

test('actual financial totals and sample-count metrics use Chinese labels', async () => {
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, models: [{ ...model, manifest: { ...model.manifest, metrics: { samples: { train: 100, validation: 20, test: 30 } } } }], backtests: [{ ...result, manifest: { ...result.manifest, metrics: { cash_interest: 50, commissions: 20, final_cash: 100, initial_cash: 1000, slippage_cost: 10, trading_days: 5, turnover: .5 } } }] } as never)
  vi.mocked(api.learningArtifact).mockResolvedValue({ ...result, manifest: { ...result.manifest, metrics: { cash_interest: 50, commissions: 20, final_cash: 100, initial_cash: 1000, slippage_cost: 10, trading_days: 5, turnover: .5 } } } as never)
  app('/learning?backtest=b1')
  for (const label of ['现金利息', '佣金总额', '期末现金', '初始资金', '滑点成本', '交易日数', '换手倍数', '训练样本数', '验证样本数', '测试样本数']) expect(await screen.findByText(label, { selector: 'dt' })).toBeInTheDocument()
})
