import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  vi.mocked(api.learningArtifact).mockResolvedValue(result as never)
  vi.mocked(api.learningChart).mockResolvedValue({ points: [{ time: '2024-07-01T01:30:00Z', equity: 100, drawdown: 0 }, { time: '2024-07-02T01:30:00Z', equity: 110, drawdown: -.01 }], total_count: 2000 })
  vi.mocked(api.learningTable).mockResolvedValue({ items: [{ order_id: 'o1', quantity: 100 }], next_cursor: 100 })
  vi.mocked(api.jobLogs).mockResolvedValue({ items: [{ seq: 1, message: '训练轮次 1', phase: 'train' }], next_cursor: null } as never)
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

test('backtest results open by URL while the combined catalog remains the result list', async () => {
  app('/learning?backtest=b1')
  const panel = await screen.findByRole('region', { name: '回测结果' })
  expect(screen.queryByRole('button', { name: '提交训练' })).toBeNull()
  expect(api.learningOverview).not.toHaveBeenCalled()
  expect(screen.getByRole('link', { name: '全部回测' })).toHaveAttribute('href', '/runs')
  expect(screen.queryByRole('button', { name: '查看结果 b1' })).toBeNull()
  expect(api.learningArtifact).toHaveBeenCalledWith('backtests', 'b1', expect.any(AbortSignal))
})

test('saved in-sample research remains visible in the selected result independently of the form', async () => {
  const research = { ...result, manifest: { ...result.manifest, metadata: { allow_in_sample: true } } }
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, backtests: [research, { ...result, id: 'b2' }] } as never)
  vi.mocked(api.learningArtifact).mockResolvedValue(research as never)
  app('/learning?backtest=b1')
  expect(screen.queryByLabelText('允许样本内研究（非独立留出证据）')).toBeNull()
  const panel = screen.getByRole('region', { name: '回测结果' })
  expect(await within(panel).findByText('样本内研究 · 非独立留出证据')).toBeInTheDocument()
  expect(api.learningOverview).not.toHaveBeenCalled()
  expect(within(panel).getByText('样本内研究 · 非独立留出证据')).toBeInTheDocument()
})

test('historical financial totals use Chinese labels', async () => {
  vi.mocked(api.learningOverview).mockResolvedValue({ ...overview, models: [{ ...model, manifest: { ...model.manifest, metrics: { samples: { train: 100, validation: 20, test: 30 } } } }], backtests: [{ ...result, manifest: { ...result.manifest, metrics: { cash_interest: 50, commissions: 20, final_cash: 100, initial_cash: 1000, slippage_cost: 10, trading_days: 5, turnover: .5 } } }] } as never)
  vi.mocked(api.learningArtifact).mockResolvedValue({ ...result, manifest: { ...result.manifest, metrics: { cash_interest: 50, commissions: 20, final_cash: 100, initial_cash: 1000, slippage_cost: 10, trading_days: 5, turnover: .5 } } } as never)
  app('/learning?backtest=b1')
  for (const label of ['现金利息', '佣金总额', '期末现金', '初始资金', '滑点成本', '交易日数', '换手倍数']) expect(await screen.findByText(label, { selector: 'dt' })).toBeInTheDocument()
})
