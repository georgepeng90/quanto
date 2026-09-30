import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import { ComparisonCharts, ResultCharts } from '../src/components/ResultCharts'

const createChart = vi.hoisted(() => vi.fn(() => ({ setOption: vi.fn(), dispose: vi.fn() })))
vi.mock('../src/components/chartEngine', () => ({ createChart }))

test('result chart initializes when IntersectionObserver is unavailable', async () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  vi.stubGlobal('fetch', vi.fn(async input => String(input) === '/api/session'
    ? Response.json({ token: 'secret', api_base: '/api', timezone: 'Asia/Shanghai' })
    : Response.json({ items: [{ date: '2024-01-01', equity: 100, benchmark_equity: 100, drawdown: 0 }], total_rows: 1, sampled: false })))
  render(<ResultCharts runId="r1" />)
  await waitFor(() => expect(createChart).toHaveBeenCalledTimes(1))
})

test('queries only the active chart summary and switches the date range on submission', async () => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} })
  const paths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async input => {
    const path = String(input)
    paths.push(path)
    if (path === '/api/session') return Response.json({ token: 'secret', api_base: '/api', timezone: 'Asia/Shanghai' })
    return Response.json({ items: [{ date: '2024-01-01', equity: 100 }], total_rows: 20000, sampled: true })
  }))
  render(<ResultCharts runId="r1" />)
  expect(await screen.findByText(/服务端已采样/)).toBeVisible()
  expect(paths.filter(path => path.includes('/charts/'))).toEqual(['/api/runs/r1/charts/curves?max_points=1200'])
  await userEvent.click(screen.getByRole('tab', { name: '月度收益' }))
  await waitFor(() => expect(paths.some(path => path.includes('/charts/monthly_returns'))).toBe(true))
  await userEvent.type(screen.getByLabelText('图表起始日'), '2024-01-01')
  await userEvent.type(screen.getByLabelText('图表结束日'), '2024-01-31')
  await userEvent.click(screen.getByRole('button', { name: '更新图表' }))
  await waitFor(() => expect(paths.some(path => path.includes('date_from=2024-01-01') && path.includes('date_to=2024-01-31'))).toBe(true))
  expect(paths.some(path => path.includes('/tables/'))).toBe(false)
  expect(screen.queryByText('查看完整图表数据')).toBeNull()
})

test('comparison overlay initializes when IntersectionObserver is unavailable', async () => {
  createChart.mockClear()
  vi.stubGlobal('IntersectionObserver', undefined)
  render(<ComparisonCharts curves={{ r1: [{ date: '2024-01-01', equity: 100, drawdown: 0 }], r2: [{ date: '2024-01-01', equity: 101, drawdown: -0.01 }] }} />)
  await waitFor(() => expect(createChart).toHaveBeenCalledTimes(1))
})
