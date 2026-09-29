import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { App } from '../src/App'
const renderApp = () => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/datasets'] })} />)

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [{ id: 'd1', content_hash: 'h', manifest: { source: 'demo', is_demo: true, price_mode: 'adjusted_research', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'unknown', limitations: ['示例数据'] } }], next_cursor: null })
    if (path === '/api/jobs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/jobs/data' && init?.method === 'POST') return Response.json({ code: 'INVALID_ARGUMENT', message: '请求参数无效', details: { reason: 'coverage incomplete' } }, { status: 422 })
    throw new Error(`unexpected ${init?.method || 'GET'} ${path}`)
  }))
})

test('shows demo, research mode, coverage and acquisition errors', async () => {
  renderApp()
  expect(await screen.findByText('演示数据')).toBeInTheDocument()
  expect(screen.getByText('复权研究价')).toBeInTheDocument()
  expect(screen.getByText(/2024-02-01.*2024-11-30/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '发起获取' }))
  await waitFor(() => expect(screen.getByText(/coverage incomplete/)).toBeInTheDocument())
})

test('never presents an incomplete finished fetch as a usable data version', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/jobs') return Response.json({ items: [{ id: 'j1', kind: 'fetch', state: 'succeeded', run_id: null, enqueue_seq: 1, heartbeat: null, completed_through: null, error: null }], next_cursor: null })
    if (path === '/api/jobs/j1') return Response.json({ id: 'j1', kind: 'fetch', state: 'succeeded', run_id: null, enqueue_seq: 1, heartbeat: null, completed_through: null, error: null, result: { state: 'incomplete', dataset_version_id: null, reason: 'COVERAGE_INCOMPLETE', coverage: { '000001': { observed: ['2024-01-02'], missing: ['2024-01-03'] } }, failed_symbols: ['000001'] } })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  expect(await screen.findByText(/尚无已验证数据版本/)).toBeInTheDocument()
  expect(screen.getByText(/覆盖不完整/)).toBeInTheDocument()
  expect(screen.getByText(/2024-01-03/)).toBeInTheDocument()
  expect(screen.queryByText(/已验证并登记数据版本/)).not.toBeInTheDocument()
})

test('labels ZIP preflight as metadata-only before queuing import', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets' && !init?.method) return Response.json({ items: [], next_cursor: null })
    if (path === '/api/jobs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/uploads') return Response.json({ upload_id: 'u1' })
    if (path === '/api/datasets/preflight') return Response.json({ price_mode: 'raw_cash', is_demo: false, event_coverage: 'complete', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', limitations: [], missing_tables: [], validation: 'metadata_only' })
    if (path === '/api/datasets' && init?.method === 'POST') return Response.json({ id: 'j1', kind: 'import', state: 'queued', run_id: null, enqueue_seq: 1, heartbeat: null, completed_through: null, error: null })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await screen.findByText(/尚无已验证数据版本/)
  await userEvent.upload(screen.getByLabelText('ZIP 文件'), new File(['zip'], 'bundle.zip', { type: 'application/zip' }))
  await userEvent.click(screen.getByRole('button', { name: '上传并预检' }))
  expect(await screen.findByText(/声明检查 · metadata_only/)).toBeInTheDocument()
  expect(screen.queryByText(/已验证并登记数据版本/)).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '提交导入验证' }))
  expect(await screen.findByText('排队中')).toBeInTheDocument()
})

test('loads later dataset and job pages with terminal job details', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [], next_cursor: '50' })
    if (path === '/api/jobs') return Response.json({ items: [], next_cursor: '50' })
    if (path === '/api/datasets?cursor=50') return Response.json({ items: [{ id: 'd2', content_hash: 'h2', manifest: { source: 'later-dataset', is_demo: false, price_mode: 'raw_cash', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'complete', limitations: [] } }], next_cursor: null })
    if (path === '/api/jobs?cursor=50') return Response.json({ items: [{ id: 'j2', kind: 'fetch', state: 'succeeded', run_id: null, enqueue_seq: 51, heartbeat: null, completed_through: null, error: null }], next_cursor: null })
    if (path === '/api/jobs/j2') return Response.json({ id: 'j2', kind: 'fetch', state: 'succeeded', run_id: null, enqueue_seq: 51, heartbeat: null, completed_through: null, error: null, result: { state: 'incomplete', dataset_version_id: null, reason: 'COVERAGE_INCOMPLETE', coverage: { '000001': { observed: [], missing: ['2024-01-03'] } } } })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await userEvent.click(await screen.findByRole('button', { name: '加载更多数据版本' }))
  await userEvent.click(screen.getByRole('button', { name: '加载更多数据任务' }))
  expect(await screen.findByText('later-dataset')).toBeInTheDocument()
  expect(screen.getByText(/2024-01-03/)).toBeInTheDocument()
})

test('discovers a new dataset page after refreshing an initially full first page', async () => {
  const manifest = { source: 'first-page', is_demo: true, price_mode: 'adjusted_research', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'unknown', limitations: [] }
  const first = Array.from({ length: 50 }, (_, index) => ({ id: `d${index}`, content_hash: 'h', manifest }))
  let reads = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') { reads += 1; return Response.json({ items: first, next_cursor: reads === 1 ? null : '50' }) }
    if (path === '/api/jobs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/datasets?cursor=50') return Response.json({ items: [{ id: 'd51', content_hash: 'h', manifest: { ...manifest, source: 'new-dataset' } }], next_cursor: null })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await waitFor(() => expect(screen.getAllByText('first-page')).toHaveLength(50))
  expect(screen.queryByRole('button', { name: '加载更多数据版本' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '刷新' }))
  await userEvent.click(await screen.findByRole('button', { name: '加载更多数据版本' }))
  expect(await screen.findByText('new-dataset')).toBeInTheDocument()
})

test('polls a submitted 51st job by ID after the first job page is full', async () => {
  const old = Array.from({ length: 50 }, (_, index) => ({ id: `j${index}`, kind: 'fetch', state: 'succeeded', run_id: null, enqueue_seq: index + 1, heartbeat: null, completed_through: null, error: null }))
  let detailReads = 0
  let listReads = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/jobs') { listReads += 1; return Response.json({ items: old, next_cursor: listReads === 1 ? null : '50' }) }
    if (path === '/api/jobs/data' && init?.method === 'POST') return Response.json({ id: 'j51', kind: 'demo', state: 'queued', enqueue_seq: 51 })
    if (path === '/api/jobs/j51') { detailReads += 1; return Response.json({ id: 'j51', kind: 'demo', state: 'succeeded', enqueue_seq: 51, result: { state: 'complete', dataset_version_id: 'd51', price_mode: 'adjusted_research' } }) }
    if (path.startsWith('/api/jobs/j')) return Response.json(old.find(job => `/api/jobs/${job.id}` === path))
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await screen.findByText('j49')
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  try {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '生成演示' })); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(detailReads).toBeGreaterThan(0)
    expect(screen.getByText(/已验证并登记数据版本 d51/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '加载更多数据任务' })).toBeInTheDocument()
  } finally { vi.useRealTimers() }
})
