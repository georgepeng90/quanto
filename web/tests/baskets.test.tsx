import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'
import { CapabilityReport } from '../src/components/CapabilityReport'

const renderBaskets = () => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/datasets?tab=baskets'] })} />)
const definitions = { items: Array.from({ length: 8 }, (_, index) => ({ ref: { kind: index === 0 ? 'trading' : 'learning', id: `etf-${index}`, version: 'v1' }, supported_instrument_types: ['etf'], implementation_hash: 'h', definition_hash: 'h', parameter_schema: {} })) }

beforeEach(() => {
  let revision = 1
  const members = [{ exchange: 'SSE', instrument_type: 'stock', code: '600000' }]
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/definitions') return Response.json(definitions)
    if (path === '/api/baskets' && init?.method === 'POST') return Response.json({ basket_id: 'b1', revision: 1, members: JSON.parse(init.body as string).members, selection_note: JSON.parse(init.body as string).selection_note, content_hash: 'h1', created_at: '2026-10-11T00:00:00Z' })
    if (path === '/api/baskets') return Response.json({ items: [{ basket_id: 'b1', name: '股票选择', current_revision: revision, created_at: '2026-10-11T00:00:00Z' }], next_cursor: null })
    if (path === '/api/baskets/b1' && !init?.method) return Response.json({ basket_id: 'b1', name: '股票选择', current_revision: revision, created_at: '2026-10-11T00:00:00Z', current: { basket_id: 'b1', revision, members, selection_note: '选择原因', content_hash: 'h1', created_at: '2026-10-11T00:00:00Z' }, revisions: [{ revision, content_hash: 'h1', created_at: '2026-10-11T00:00:00Z' }] })
    if (path === '/api/baskets/b1/revisions' && init?.method === 'POST') { const body = JSON.parse(init.body as string); revision += 1; return Response.json({ basket_id: 'b1', revision, members: body.members, selection_note: body.selection_note, content_hash: 'h2', created_at: '2026-10-11T00:00:01Z' }) }
    throw new Error(`unexpected ${path}`)
  }))
})

test('four domains and stock basket revision preserve canonical ordered members and CAS', async () => {
  renderBaskets()
  expect(screen.getByRole('navigation', { name: '主导航' }).querySelectorAll('a')).toHaveLength(4)
  expect([...screen.getByRole('navigation', { name: '主导航' }).querySelectorAll('a')].map(item => item.textContent)).toEqual(['数据', '固定策略', '强化学习模型', '回测'])
  await userEvent.click(await screen.findByRole('button', { name: '股票选择' }))
  expect(screen.getByText(/没有股票运行方案/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '添加成员' }))
  await userEvent.selectOptions(screen.getAllByLabelText('类型')[1], 'etf')
  await userEvent.clear(screen.getAllByLabelText('代码')[1]); await userEvent.type(screen.getAllByLabelText('代码')[1], '512480')
  await userEvent.click(screen.getByRole('button', { name: '上移成员 2' }))
  await userEvent.click(screen.getByRole('button', { name: '保存组合' }))
  await waitFor(() => expect(screen.getByText(/已保存 · SSE:etf:512480/)).toBeInTheDocument())
  expect(screen.getByRole('button', { name: '股票选择' })).toBeInTheDocument()
  const call = vi.mocked(fetch).mock.calls.find(([url]) => url === '/api/baskets/b1/revisions')!
  expect(JSON.parse(call[1]!.body as string)).toEqual({ expected_revision: 1, members: [{ exchange: 'SSE', instrument_type: 'etf', code: '512480' }, { exchange: 'SSE', instrument_type: 'stock', code: '600000' }], selection_note: '选择原因' })
  expect(screen.getByText(/混合篮子运行需通过服务端能力检查/)).toBeInTheDocument()
})

test('server capability report renders field paths, reasons and limitations', () => {
  render(<CapabilityReport report={{ operation: 'train', status: 'blocked', request_hash: null, dataset_version_id: 'd1', input_hashes: {}, checks: [{ name: 'definition', status: 'blocked', field_path: 'learning_definition_ref', code: 'DEFINITION_UNAVAILABLE', message: '股票方案未接入' }], assumptions: ['人工选择待核验'], limitations: ['缺财务事实'], checker_version: 'v1', generated_at: null }} />)
  expect(screen.getAllByText(/股票方案未接入/).length).toBeGreaterThan(0)
  expect(screen.getAllByText(/缺财务事实/).length).toBeGreaterThan(0)
})

test('manual stock selection saves without an installed stock run definition and retries create with one key', async () => {
  let posts = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/definitions') return Response.json(definitions)
    if (path === '/api/baskets' && init?.method === 'POST') { posts += 1; if (posts === 1) throw new Error('timeout'); return Response.json({ basket_id: 'b1', revision: 1, members: JSON.parse(init.body as string).members, selection_note: '自选', content_hash: 'h', created_at: '2026-10-11T00:00:00Z' }) }
    if (path === '/api/baskets') return Response.json({ items: posts > 1 ? [{ basket_id: 'b1', name: '股票篮子', current_revision: 1, created_at: '2026-10-11T00:00:00Z' }] : [], next_cursor: null })
    if (path === '/api/baskets/b1') return Response.json({ basket_id: 'b1', name: '股票篮子', current_revision: 1, created_at: '2026-10-11T00:00:00Z', current: { basket_id: 'b1', revision: 1, members: [{ exchange: 'SSE', instrument_type: 'stock', code: '600000' }], selection_note: '自选', content_hash: 'h', created_at: '2026-10-11T00:00:00Z' }, revisions: [{ revision: 1, content_hash: 'h', created_at: '2026-10-11T00:00:00Z' }] })
    throw new Error(`unexpected ${path}`)
  }))
  renderBaskets()
  await userEvent.type(screen.getByLabelText('篮子名称'), '股票篮子')
  await userEvent.selectOptions(screen.getByLabelText('类型'), 'stock')
  await userEvent.type(screen.getByLabelText('代码'), '600000')
  await userEvent.type(screen.getByLabelText('选择说明'), '自选')
  await userEvent.click(screen.getByRole('button', { name: '保存组合' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('timeout')
  await userEvent.click(screen.getByRole('button', { name: '保存组合' }))
  expect(await screen.findByText(/已保存 · SSE:stock:600000/)).toBeInTheDocument()
  const bodies = vi.mocked(fetch).mock.calls.filter(([url, init]) => url === '/api/baskets' && init?.method === 'POST').map(([, init]) => JSON.parse(init!.body as string))
  expect(bodies).toHaveLength(2)
  expect(bodies[0]).toEqual({ name: '股票篮子', members: [{ exchange: 'SSE', instrument_type: 'stock', code: '600000' }], selection_note: '自选', idempotency_key: bodies[1].idempotency_key })
  expect(screen.getByText(/没有股票运行方案/)).toBeInTheDocument()
})

test.each(['list', 'detail'])('created basket stays saved when the follow-up %s read fails', async failure => {
  let lists = 0
  let posts = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/definitions') return Response.json(definitions)
    if (path === '/api/baskets' && init?.method === 'POST') { posts += 1; return Response.json({ basket_id: 'b1', revision: 1, members: [{ exchange: 'SSE', instrument_type: 'etf', code: '512480' }], selection_note: '筛选', content_hash: 'h', created_at: '2026-10-11T00:00:00Z' }) }
    if (path === '/api/baskets') { lists += 1; if (failure === 'list' && lists > 1) throw new Error('list timeout'); return Response.json({ items: posts ? [{ basket_id: 'b1', name: 'ETF 篮子', current_revision: 1, created_at: '2026-10-11T00:00:00Z' }] : [], next_cursor: null }) }
    if (path === '/api/baskets/b1') { if (failure === 'detail') throw new Error('detail timeout'); return Response.json({ basket_id: 'b1', name: 'ETF 篮子', current_revision: 1, created_at: '2026-10-11T00:00:00Z', current: { basket_id: 'b1', revision: 1, members: [{ exchange: 'SSE', instrument_type: 'etf', code: '512480' }], selection_note: '筛选', content_hash: 'h', created_at: '2026-10-11T00:00:00Z' }, revisions: [{ revision: 1, content_hash: 'h', created_at: '2026-10-11T00:00:00Z' }] }) }
    throw new Error(`unexpected ${path}`)
  }))
  renderBaskets()
  await userEvent.type(screen.getByLabelText('篮子名称'), 'ETF 篮子')
  await userEvent.type(screen.getByLabelText('代码'), '512480')
  await userEvent.type(screen.getByLabelText('选择说明'), '筛选')
  await userEvent.click(screen.getByRole('button', { name: '保存组合' }))
  expect(await screen.findByText(/已保存 · SSE:etf:512480/)).toBeInTheDocument()
  expect(posts).toBe(1)
  expect(screen.getByRole('button', { name: '保存组合' })).toBeInTheDocument()
})
