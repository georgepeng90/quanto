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
const app = (path = '/investing') => render(<MemoryRouter initialEntries={[path]}><InvestingPage /></MemoryRouter>)
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
test('historical daily deep links expose report detail only', async () => {
  app('/investing?backtest=b1')
  const panel = screen.getByRole('region', { name: '日线回测结果' })
  expect(await within(panel).findByRole('img', { name: '每日净值曲线' })).toBeInTheDocument()
  fireEvent.change(within(panel).getByLabelText('结果明细'), { target: { value: 'positions' } })
  await waitFor(() => expect(api.investingTable).toHaveBeenLastCalledWith('b1', 'positions', 0, expect.any(AbortSignal)))
  expect(screen.getByRole('link', { name: '全部回测' })).toHaveAttribute('href', '/runs')
  expect(screen.queryByRole('button', { name: '提交训练' })).toBeNull()
  expect(screen.queryByLabelText('训练开始')).toBeNull()
  expect(api.investingOverview).not.toHaveBeenCalled()
})
