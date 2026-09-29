import { render, waitFor } from '@testing-library/react'
import { expect, test, vi } from 'vitest'
import { ComparisonCharts, ResultCharts } from '../src/components/ResultCharts'

const createChart = vi.hoisted(() => vi.fn(() => ({ setOption: vi.fn(), dispose: vi.fn() })))
vi.mock('../src/components/chartEngine', () => ({ createChart }))

test('result chart initializes when IntersectionObserver is unavailable', async () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  render(<ResultCharts curves={[{ date: '2024-01-01', equity: 100, benchmark_equity: 100, drawdown: 0 }]} monthly={[]} annual={[]} positions={[]} />)
  await waitFor(() => expect(createChart).toHaveBeenCalledTimes(1))
})

test('comparison overlay initializes when IntersectionObserver is unavailable', async () => {
  createChart.mockClear()
  vi.stubGlobal('IntersectionObserver', undefined)
  render(<ComparisonCharts curves={{ r1: [{ date: '2024-01-01', equity: 100, drawdown: 0 }], r2: [{ date: '2024-01-01', equity: 101, drawdown: -0.01 }] }} />)
  await waitFor(() => expect(createChart).toHaveBeenCalledTimes(1))
})
