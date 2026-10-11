import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'

const spec = { start: '2024-02-01', end: '2024-03-01', initial_cash: 1000, seed: 7, execution: { commission_rate: 0, minimum_commission: 0, slippage_rate: 0, max_stale_sessions: 0, model: 'next_open' }, risk: { max_total_weight: 1, max_symbol_weight: 1, group_caps: {}, stress_scenarios: [], pause_action: 'block_new', recovery_mode: 'never' }, metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: 'v1' } }
const draft = (id: string) => ({ id, revision: 2, payload: { framework_contract_version: 'multi-asset-v1', basket_revision_ref: { basket_id: 'b1', revision: 1 }, trading_definition_ref: { kind: 'trading', id: 'etf-trading', version: 'v1' }, strategy_revision_id: 's1', dataset_version_id: 'dataset-123456789', strategy_params: { window: 4 }, backtest_spec: spec, sample_purpose: '探索' } })
const basket = { basket_id: 'b1', revision: 1, members: [{ exchange: 'SSE', instrument_type: 'etf', code: '512480' }], created_at: '', selection_note: '研究选择', content_hash: 'h' }
const strategy = { id: 's1', content_hash: 'hash', manifest: { name: '均线策略', entrypoint: 'strategy.py:create_strategy', parameter_schema: { type: 'object', properties: { window: { type: 'integer', title: '窗口' } } }, auxiliary_sources: [], research_data: [] }, files: {} }
const dataset = { id: 'dataset-123456789', content_hash: 'hash', manifest: { source: 'sample', is_demo: true, price_mode: 'adjusted_research', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'unknown', limitations: [] } }
const renderAt = (path = '/experiments') => {
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })
  render(<RouterProvider router={router} />)
  return router
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/experiments') return Response.json({ items: [draft('e1'), draft('e2')], next_cursor: null })
    if (path === '/api/strategies') return Response.json({ items: [strategy], next_cursor: null })
    if (path === '/api/experiments/e1' || path === '/api/experiments/e2') return Response.json(draft(path.slice(-2)))
    if (path === '/api/strategies/s1') return Response.json(strategy)
    if (path === '/api/baskets/b1/revisions/1') return Response.json(basket)
    if (path === '/api/baskets') return Response.json({ items: [{ basket_id: 'b1', name: 'ETF 篮子', current_revision: 1, created_at: '' }], next_cursor: null })
    if (path === '/api/baskets/b1') return Response.json({ basket_id: 'b1', name: 'ETF 篮子', current_revision: 1, created_at: '', current: basket, revisions: [{ revision: 1, content_hash: 'h', created_at: '' }] })
    if (path === '/api/definitions') return Response.json({ items: [{ ref: { kind: 'trading', id: 'etf-trading', version: 'v1' } }] })
    if (path === '/api/datasets') return Response.json({ items: [dataset], next_cursor: null })
    if (path === `/api/datasets/${dataset.id}`) return Response.json(dataset)
    throw new Error(`unexpected GET ${path}`)
  }))
})

test('lists each saved framework configuration with requested parameters and basket identity', async () => {
  const router = renderAt()
  expect(await screen.findByRole('heading', { name: '固定策略' })).toBeInTheDocument()
  const rows = await screen.findAllByRole('link', { name: '编辑配置' })
  expect(rows).toHaveLength(2)
  expect(screen.getAllByText('均线策略')).toHaveLength(2)
  expect(screen.getAllByText('窗口：4')).toHaveLength(2)
  expect(screen.getAllByText(/SSE:etf:512480/)).toHaveLength(2)
  expect(screen.getAllByRole('article')[0]).toHaveTextContent('ETF 篮子')
  expect(screen.getAllByRole('article')[1]).toHaveTextContent('ETF 篮子')
  expect(screen.getAllByText(/2024-02-01.*2024-03-01/)).toHaveLength(2)
  await userEvent.click(rows[1])
  expect(router.state.location.pathname).toBe('/experiments/e2')
  expect(await screen.findByRole('heading', { name: '编辑固定策略' })).toBeInTheDocument()
})

test('opens a separate new configuration page', async () => {
  const router = renderAt()
  await userEvent.click(await screen.findByRole('link', { name: '新建固定策略' }))
  expect(router.state.location.pathname).toBe('/experiments/new')
  expect(await screen.findByRole('heading', { name: '新建固定策略' })).toBeInTheDocument()
})

test('keeps a saved configuration visible when its strategy metadata fails', async () => {
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/strategies') return Response.json({ items: [], next_cursor: '50' })
    if (path === '/api/strategies/s1') return Response.json({ code: 'NOT_FOUND', message: 'gone' }, { status: 404 })
    return previous(input, init)
  }))
  renderAt()
  expect(await screen.findAllByText('已保存策略')).toHaveLength(2)
  expect(screen.getAllByText('window：4')).toHaveLength(2)
  expect(screen.getAllByRole('link', { name: '编辑配置' })).toHaveLength(2)
})

test('keeps saved configurations visible when the strategy listing fails', async () => {
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/strategies') return Response.json({ code: 'UNAVAILABLE', message: 'unavailable' }, { status: 503 })
    return previous(input, init)
  }))
  renderAt()
  expect(await screen.findAllByRole('link', { name: '编辑配置' })).toHaveLength(2)
  expect(screen.getAllByText('均线策略')).toHaveLength(2)
})

test('loads later saved configurations without merging identical strategy parameters', async () => {
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/experiments') return Response.json({ items: [draft('e1')], next_cursor: '50' })
    if (path === '/api/experiments?cursor=50') return Response.json({ items: [draft('e2')], next_cursor: null })
    return previous(input, init)
  }))
  renderAt()
  expect(await screen.findAllByRole('link', { name: '编辑配置' })).toHaveLength(1)
  await userEvent.click(screen.getByRole('button', { name: '加载更多配置' }))
  expect(await screen.findAllByRole('link', { name: '编辑配置' })).toHaveLength(2)
})

test('offers a retry after the configuration list fails', async () => {
  let attempts = 0
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/experiments' && ++attempts === 1) return Response.json({ code: 'UNAVAILABLE', message: 'try again' }, { status: 503 })
    return previous(input, init)
  }))
  renderAt()
  expect(await screen.findByRole('alert')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(await screen.findAllByRole('link', { name: '编辑配置' })).toHaveLength(2)
  expect(attempts).toBe(2)
})

test('shows an empty state without exposing unsaved strategy templates', async () => {
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/experiments') return Response.json({ items: [], next_cursor: null })
    return previous(input, init)
  }))
  renderAt()
  expect(await screen.findByText(/暂无固定策略配置/)).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: '编辑配置' })).not.toBeInTheDocument()
})
