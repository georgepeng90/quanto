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
  expect(screen.getByText('dataset_version_id')).toBeVisible()
  expect(screen.getByText(/d1/)).toBeVisible()
  expect(screen.getByText(/d2/)).toBeVisible()
  expect(screen.queryByRole('columnheader', { name: /排名|最佳/ })).toBeNull()
  expect(screen.getByText('不可用')).toBeVisible()
})

test('loads complete comparable curves across cursor pages for overlay', async () => {
  const prior = globalThis.fetch
  const paths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    paths.push(path)
    if (path === '/api/comparisons') return Response.json({ runs: [{ ...base, run_id: 'r1' }, { ...base, run_id: 'r2', metrics: { total_return: 0.2, max_drawdown: 0.1, sharpe: 1.2 } }], differences: { effective_strategy_params: [{ window: 4 }, { window: 8 }] }, directly_comparable: true })
    if (path.endsWith('/tables/curves')) return Response.json({ items: Array.from({ length: 100 }, (_, index) => ({ date: `2024-01-${String(index + 1).padStart(3, '0')}`, equity: 100 + index, drawdown: -0.01 })), next_cursor: '100' })
    if (path.endsWith('/tables/curves?cursor=100')) return Response.json({ items: [{ date: '2024-05-01', equity: 250, drawdown: -0.2 }], next_cursor: null })
    return prior(input, init)
  }))
  render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/compare?run_ids=r1,r2'] })} />)
  await userEvent.click(await screen.findByRole('button', { name: '比较运行' }))
  expect((await screen.findAllByText(/2024-05-01/)).length).toBe(2)
  expect(paths).toContain('/api/runs/r1/tables/curves?cursor=100')
  expect(paths).toContain('/api/runs/r2/tables/curves?cursor=100')
})
