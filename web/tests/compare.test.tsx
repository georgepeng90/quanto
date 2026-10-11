import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'

const base = { state: 'succeeded', metrics: { total_return: 0.1, max_drawdown: 0.2, sharpe: null }, metric_definitions: { total_return: { description: '累计收益' } }, strategy_revision_id: 's1', effective_strategy_params: { window: 4 }, dataset_version_id: 'd1', price_mode: 'raw_cash', start: '2024-01-01', end: '2024-01-31', environment_fingerprint: 'env1' }
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/session') return Response.json({ token: 'secret', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (String(input) === '/api/comparisons') {
      expect(JSON.parse(String(init?.body)).run_ids).toEqual(['r1', 'r2'])
      return Response.json({ runs: [{ ...base, run_id: 'r1' }, { ...base, run_id: 'r2', dataset_version_id: 'd2', metrics: { total_return: 0.4, max_drawdown: 0.3, sharpe: 1.2 } }], differences: { dataset_version_id: ['d1', 'd2'] }, directly_comparable: false })
    }
    throw new Error(`unexpected ${String(input)}`)
  }))
})

test('does not rank incompatible runs and displays supplied differences', async () => {
  render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/compare?run_ids=r1,r2'] })} />)
  await userEvent.click(await screen.findByRole('button', { name: '比较运行' }))
  expect(await screen.findByText(/口径不可直接比较/)).toBeVisible()
  await userEvent.click(screen.getByText('技术诊断差异'))
  expect(screen.getByText('dataset_version_id')).toBeVisible()
  expect(screen.getByText(/d1/)).toBeVisible()
  expect(screen.getByText(/d2/)).toBeVisible()
  expect(screen.queryByRole('columnheader', { name: /排名|最佳/ })).toBeNull()
  expect(screen.getByText('不可用')).toBeVisible()
})

test('loads one bounded chart summary per comparable run', async () => {
  const prior = globalThis.fetch
  const paths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    paths.push(path)
    if (path === '/api/comparisons') return Response.json({ runs: [{ ...base, run_id: 'r1' }, { ...base, run_id: 'r2', metrics: { total_return: 0.2, max_drawdown: 0.1, sharpe: 1.2 } }], differences: { effective_strategy_params: [{ window: 4 }, { window: 8 }] }, directly_comparable: true })
    if (path.includes('/charts/curves')) return Response.json({ items: [{ date: '2024-05-01', equity: 250, drawdown: -0.2 }], total_rows: 50000, sampled: true })
    return prior(input, init)
  }))
  render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/compare?run_ids=r1,r2'] })} />)
  await userEvent.click(await screen.findByRole('button', { name: '比较运行' }))
  await screen.findByRole('heading', { name: '可比运行曲线' })
  await userEvent.click(screen.getByText('图表摘要数据'))
  expect(await screen.findByRole('cell', { name: '2024-05-01' })).toBeVisible()
  await userEvent.selectOptions(screen.getByLabelText('图表数据运行'), 'r2')
  expect(screen.getByRole('cell', { name: '250' })).toBeVisible()
  expect(paths.filter(path => path.includes('/charts/'))).toEqual(['/api/runs/r1/charts/curves?max_points=1200', '/api/runs/r2/charts/curves?max_points=1200'])
  expect(paths.some(path => path.includes('/tables/'))).toBe(false)
})


test('comparison shows demo disclosure, complete configuration labels and readable metric units', async () => {
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/comparisons') return Response.json({ runs: [{ ...base, run_id: 'r1', is_demo: true, source: 'fixture', currency: 'CNY', metrics: { total_return: .123456, final_equity: 1234.567, benchmark_return: .12, relative_return_difference: .003456 } }, { ...base, run_id: 'r2', is_demo: false, source: 'real' }], differences: { symbols: [['A'], ['B']], evaluation: [{ cash_rate: 0 }, { cash_rate: .1 }], backtest_spec: [{}, {}] }, directly_comparable: false })
    return previous(input, init)
  }))
  render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/compare?run_ids=r1,r2'] })} />)
  await userEvent.click(await screen.findByRole('button', { name: '比较运行' }))
  expect(await screen.findByText(/演示数据：合成绩效/)).toBeVisible()
  expect(screen.getByText('基准收益')).toBeVisible()
  expect(screen.getByText('相对收益差')).toBeVisible()
  expect(screen.getByText('12.35%')).toBeVisible()
  expect(screen.getByText('CNY 1,234.57')).toBeVisible()
  expect(screen.getByText(/标的池/)).toBeVisible()
  expect(screen.getByText(/评估与现金利息/)).toBeVisible()
})
