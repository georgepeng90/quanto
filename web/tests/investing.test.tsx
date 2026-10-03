import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { InvestingPage } from '../src/pages/InvestingPage'
import { api } from '../src/api'

vi.mock('../src/api', () => ({ api: { investingOverview: vi.fn(), dataset: vi.fn(), trainInvesting: vi.fn(), backtestInvesting: vi.fn(), investingArtifact: vi.fn(), investingChart: vi.fn(), investingTable: vi.fn(), exportInvesting: vi.fn(), jobLogs: vi.fn(), cancelJob: vi.fn() }, describeError: (error: Error) => error.message }))
const policy = { id: 'p1', kind: 'policy', manifest: { config: { name: '周度 PPO', validation_end: '2023-12-31' }, metrics: { validation: { total_return: .1 }, test: { total_return: .05 } } } }
const backtest = { id: 'b1', kind: 'policy_backtest', manifest: { metrics: { total_return: .2 }, metadata: { baseline: 'policy', cost_multiplier: 1 } } }
const overview = { capabilities: { torch_available: true, cuda_available: false, device_name: 'CPU' }, datasets: [{ id: 'd1', manifest: { data_start: '2020-01-01', data_end: '2025-12-31', price_mode: 'adjusted_research', limitations: ['研究复权价格'] }, instruments: [{ symbol: 'ETF1' }] }], policies: [policy], backtests: [backtest], jobs: [] }
const dates = ['2021-01-01', '2022-12-31', '2023-01-01', '2023-12-31', '2024-01-01', '2024-12-31']
const app = (path = '/investing') => render(<MemoryRouter initialEntries={[path]}><InvestingPage /></MemoryRouter>)
function setDates() { ['训练开始', '训练结束', '验证开始', '验证结束', '测试开始', '测试结束'].forEach((label, index) => fireEvent.change(screen.getByLabelText(label), { target: { value: dates[index] } })) }
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.investingOverview).mockResolvedValue(overview as never)
  vi.mocked(api.dataset).mockResolvedValue(overview.datasets[0] as never)
  vi.mocked(api.trainInvesting).mockResolvedValue({ id: 't1', kind: 'train_policy', state: 'queued' } as never)
  vi.mocked(api.backtestInvesting).mockResolvedValue({ id: 'b2', kind: 'policy_backtest', state: 'queued' } as never)
  vi.mocked(api.investingArtifact).mockImplementation(async resource => (resource === 'policies' ? policy : backtest) as never)
  vi.mocked(api.investingChart).mockResolvedValue({ points: [{ time: '2024-01-02', equity: 100, drawdown: 0 }, { time: '2024-01-03', equity: 110, drawdown: 0 }], total_count: 2 })
  vi.mocked(api.investingTable).mockResolvedValue({ items: [{ symbol: 'ETF1', quantity: 100 }], next_cursor: null })
  vi.mocked(api.jobLogs).mockResolvedValue({ items: [{ seq: 1, phase: 'train', message: 'PPO 更新 128' }], next_cursor: null } as never)
})
test('trains daily observations on weekly actions with ordered inclusive calendar dates', async () => {
  app()
  await screen.findByRole('option', { name: /d1/ })
  setDates()
  fireEvent.change(screen.getByLabelText('验证开始'), { target: { value: '2022-12-31' } })
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('不重叠')
  expect(api.trainInvesting).not.toHaveBeenCalled()
  setDates()
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  await waitFor(() => expect(api.trainInvesting).toHaveBeenCalledWith(expect.objectContaining({ dataset_id: 'd1', spec: expect.objectContaining({ train_start: dates[0], test_end: dates[5], frequency: 'weekly', gamma: 1, backtest: expect.objectContaining({ symbols: ['ETF1'] }) }) })))
  expect(await screen.findByText(/任务 t1 已提交/)).toBeInTheDocument()
})
test('requires a frozen experiment reference for cash and blocks validation overlap', async () => {
  app()
  await screen.findByRole('option', { name: /d1/ })
  fireEvent.change(screen.getByLabelText('回测开始'), { target: { value: '2023-12-31' } })
  fireEvent.change(screen.getByLabelText('回测结束'), { target: { value: '2024-12-31' } })
  fireEvent.change(screen.getByLabelText('回测方法'), { target: { value: 'cash' } })
  await userEvent.click(screen.getByRole('button', { name: '提交回测' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('验证截止')
  expect(api.backtestInvesting).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('回测开始'), { target: { value: '2024-01-01' } })
  await userEvent.click(screen.getByRole('button', { name: '提交回测' }))
  expect(api.backtestInvesting).toHaveBeenCalledWith({ dataset_id: 'd1', policy_id: 'p1', spec: { start: '2024-01-01', end: '2024-12-31', baseline: 'cash', cost_multiplier: 1 } })
})
test('shows validation/test separately and exposes daily curve and holdings', async () => {
  app('/investing?backtest=b1')
  await userEvent.click(await screen.findByRole('button', { name: '查看策略 p1' }))
  expect(await screen.findByText('验证段指标')).toBeInTheDocument()
  expect(screen.getByText('测试段指标')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '查看结果 b1' })).toBeNull()
  const panel = screen.getByRole('region', { name: '日线回测结果' })
  expect(panel.compareDocumentPosition(screen.getByRole('heading', { name: '1 · 日线数据版本' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(await within(panel).findByRole('img', { name: '每日净值曲线' })).toBeInTheDocument()
  fireEvent.change(within(panel).getByLabelText('结果明细'), { target: { value: 'positions' } })
  await waitFor(() => expect(api.investingTable).toHaveBeenLastCalledWith('b1', 'positions', 0, expect.any(AbortSignal)))
  expect(within(panel).getByRole('button', { name: '下载完整 ZIP' })).toBeInTheDocument()
})
test('active policy jobs show logs and cancel progress', async () => {
  vi.mocked(api.investingOverview).mockResolvedValue({ ...overview, jobs: [{ id: 't1', kind: 'train_policy', state: 'running' }] } as never)
  vi.mocked(api.cancelJob).mockResolvedValue({ id: 't1', kind: 'train_policy', state: 'cancel_requested' } as never)
  app()
  await userEvent.click(await screen.findByRole('button', { name: '查看日志 t1' }))
  expect(await screen.findByText('PPO 更新 128')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '取消 t1' }))
  expect(await screen.findByText('取消中')).toBeInTheDocument()
})
test('server failures are shown and overview polling aborts on unmount', async () => {
  vi.mocked(api.trainInvesting).mockRejectedValue(new Error('交易日覆盖不足'))
  const view = app()
  await screen.findByRole('option', { name: /d1/ })
  setDates()
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('交易日覆盖不足')
  const signal = vi.mocked(api.investingOverview).mock.calls[0][0]!
  view.unmount()
  expect(signal.aborted).toBe(true)
})

test('rejects zero reward discount before dispatching training', async () => {
  app()
  await screen.findByRole('option', { name: /d1/ })
  setDates()
  fireEvent.change(screen.getByLabelText('奖励折扣'), { target: { value: '0' } })
  await userEvent.click(screen.getByRole('button', { name: '提交训练' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('奖励折扣')
  expect(api.trainInvesting).not.toHaveBeenCalled()
})

test('retains daily metrics when charts fail and paginates detailed fills', async () => {
  vi.mocked(api.investingChart).mockRejectedValue(new Error('曲线读取失败'))
  vi.mocked(api.investingTable).mockResolvedValue({ items: [{ symbol: 'ETF1', quantity: 100 }], next_cursor: '100' })
  app('/investing?backtest=b1')
  const panel = screen.getByRole('region', { name: '日线回测结果' })
  expect(await within(panel).findByText('20.00%')).toBeInTheDocument()
  expect(within(panel).getByRole('alert')).toHaveTextContent('曲线读取失败')
  fireEvent.change(within(panel).getByLabelText('结果明细'), { target: { value: 'fills' } })
  await userEvent.click(await within(panel).findByRole('button', { name: '下一页明细' }))
  await waitFor(() => expect(api.investingTable).toHaveBeenLastCalledWith('b1', 'fills', '100', expect.any(AbortSignal)))
})
