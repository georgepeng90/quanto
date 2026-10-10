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
    if (path === '/api/capabilities/check') return Response.json({ operation: 'fetch', status: 'ready', request_hash: 'h', dataset_version_id: null, input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null })
    if (path === '/api/jobs/data' && init?.method === 'POST') return Response.json({ code: 'INVALID_ARGUMENT', message: '请求参数无效', details: { reason: 'coverage incomplete' } }, { status: 422 })
    throw new Error(`unexpected ${init?.method || 'GET'} ${path}`)
  }))
})

test('shows registered provenance and acquisition errors without demo generation', async () => {
  renderApp()
  expect(await screen.findByText('演示数据')).toBeInTheDocument()
  expect(screen.getByText('复权研究价')).toBeInTheDocument()
  expect(screen.getByText(/2024-02-01.*2024-11-30/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '生成演示' })).not.toBeInTheDocument()
  expect(screen.queryByLabelText('年数')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('种子')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '发起获取' }))
  await waitFor(() => expect(screen.getByText(/coverage incomplete/)).toBeInTheDocument())
})

test('never presents an incomplete finished fetch as a usable data version', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/jobs') return Response.json({ items: [{ id: 'j1', kind: 'fetch', state: 'succeeded', run_id: null, enqueue_seq: 1, heartbeat: null, completed_through: null, error: null }], next_cursor: null })
    if (path === '/api/jobs/j1') return Response.json({ id: 'j1', kind: 'fetch', state: 'succeeded', run_id: null, enqueue_seq: 1, heartbeat: null, completed_through: null, error: null, result: { state: 'incomplete', market_prefetch_state: 'incomplete', dataset_version_id: null, reason: 'COVERAGE_INCOMPLETE', coverage: { entries: [{ missing_by_instrument: { 'SZSE:etf:000001': ['2024-01-03'] } }] } } })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  expect(await screen.findByText(/尚无已验证数据版本/)).toBeInTheDocument()
  expect(screen.queryByText(/可生成演示数据/)).not.toBeInTheDocument()
  expect(screen.getByText(/覆盖不完整/)).toBeInTheDocument()
  expect(screen.getByText(/2024-01-03/)).toBeInTheDocument()
  expect(screen.queryByText(/已验证并登记数据版本/)).not.toBeInTheDocument()
})

test.each([false, true])('fetch submits requirements with explicit refresh=%s', async refresh => {
  renderApp()
  await screen.findByText('演示数据')
  expect(screen.queryByLabelText('缓存版本（可选）')).not.toBeInTheDocument()
  await userEvent.type(screen.getByLabelText('标的代码'), '512480')
  fireEvent.change(screen.getByLabelText('起始日'), { target: { value: '2024-01-01' } })
  fireEvent.change(screen.getByLabelText('结束日'), { target: { value: '2024-01-05' } })
  if (refresh) await userEvent.click(screen.getByLabelText('更新已有行情'))
  await userEvent.click(screen.getByRole('button', { name: '发起获取' }))
  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/jobs/data')).toBe(true))
  const [, init] = vi.mocked(fetch).mock.calls.find(([url]) => url === '/api/jobs/data')!
  expect(JSON.parse(init!.body as string)).toEqual({ kind: 'fetch', payload: { instruments: [{ exchange: 'SSE', instrument_type: 'etf', code: '512480' }], benchmark: { exchange: 'SSE', instrument_type: 'index', code: '000300' }, start_date: '2024-01-01', end_date: '2024-01-05', refresh } })
})

test.each([
  ['ready', 'd1', true], ['ready', null, false], ['incomplete', 'forged', false], ['failed', null, false], ['cancelled', null, false],
])('fetch readiness %s and dataset %s gates usable result', async (state, version, usable) => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [], next_cursor: null })
    const job = { id: 'j-ready', kind: 'fetch', state: 'succeeded', result: { state: 'complete', market_prefetch_state: state, dataset_version_id: version, limitations: ['source limitation'] } }
    if (path === '/api/jobs') return Response.json({ items: [job], next_cursor: null })
    if (path === '/api/jobs/j-ready') return Response.json(job)
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await screen.findByText('j-ready')
  expect(Boolean(screen.queryByText(/已验证并登记数据版本/))).toBe(usable)
  expect(screen.getByText(/source limitation/)).toBeInTheDocument()
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
    if (path === '/api/capabilities/check') return Response.json({ operation: 'fetch', status: 'ready', request_hash: 'h', dataset_version_id: null, input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null })
    if (path === '/api/jobs/data' && init?.method === 'POST') return Response.json({ id: 'j51', kind: 'fetch', state: 'queued', enqueue_seq: 51 })
    if (path === '/api/jobs/j51') { detailReads += 1; return Response.json({ id: 'j51', kind: 'fetch', state: 'succeeded', enqueue_seq: 51, result: { state: 'complete', market_prefetch_state: 'ready', dataset_version_id: 'd51', price_mode: 'adjusted_research' } }) }
    if (path.startsWith('/api/jobs/j')) return Response.json(old.find(job => `/api/jobs/${job.id}` === path))
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await screen.findByText('j49')
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
  try {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发起获取' })); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(detailReads).toBeGreaterThan(0)
    expect(screen.getByText(/已验证并登记数据版本 d51/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '加载更多数据任务' })).toBeInTheDocument()
  } finally { vi.useRealTimers() }
})

test('binding submits only the saved evidence reference and reuses its key after a timeout', async () => {
  let detailReads = 0
  let bindCalls = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [{ id: 'd1', content_hash: 'h', manifest: { source: 'import', is_demo: false, price_mode: 'raw_cash', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'complete', limitations: [] } }], next_cursor: null })
    if (path === '/api/jobs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/datasets/d1' && !init?.method) { detailReads += 1; return Response.json({ identity_evidence_ref: 'import:proof', identity_binding_issue: { code: 'IDENTITY_UNRESOLVED', message: '待绑定' }, asset_binding: detailReads > 1 ? { binding_hash: 'bound', assets: [{ instrument_id: { exchange: 'SSE', instrument_type: 'stock', code: '600000' } }] } : null }) }
    if (path === '/api/datasets/d1/asset-binding') { bindCalls += 1; if (bindCalls === 1) throw new Error('timeout'); return Response.json({ binding_hash: 'bound' }) }
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await userEvent.click(await screen.findByRole('button', { name: '查看身份' }))
  await userEvent.click(await screen.findByRole('button', { name: '检查并关联身份' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('timeout')
  await userEvent.click(screen.getByRole('button', { name: '检查并关联身份' }))
  expect(await screen.findByText(/已绑定 · bound/)).toBeInTheDocument()
  const bodies = vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/datasets/d1/asset-binding').map(([, init]) => JSON.parse(init!.body as string))
  expect(bodies).toHaveLength(2)
  expect(bodies[0]).toEqual({ evidence_ref: 'import:proof', idempotency_key: bodies[1].idempotency_key })
})

test('missing evidence offers reimport without arbitrary mapping controls', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [{ id: 'd1', content_hash: 'h', manifest: { source: 'legacy', is_demo: false, price_mode: 'raw_cash', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'unknown', limitations: [] } }], next_cursor: null })
    if (path === '/api/jobs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/datasets/d1') return Response.json({ identity_evidence_ref: null, identity_binding_issue: { code: 'IDENTITY_UNRESOLVED', message: '来源不明' }, asset_binding: null })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await userEvent.click(await screen.findByRole('button', { name: '查看身份' }))
  expect(await screen.findByText(/请重新导入包含 identity_evidence.json/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '检查并关联身份' })).not.toBeInTheDocument()
})

test('blocked fetch shows server reasons and does not queue a data job', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets' || path === '/api/jobs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/capabilities/check') return Response.json({ operation: 'fetch', status: 'blocked', request_hash: null, dataset_version_id: null, input_hashes: {}, checks: [{ name: 'source', status: 'blocked', field_path: 'instruments.0', code: 'DEFINITION_UNAVAILABLE', message: '股票数据源未接入' }], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await screen.findByText(/尚无已验证数据版本/)
  await userEvent.selectOptions(screen.getByLabelText('标的类型'), 'stock')
  await userEvent.type(screen.getByLabelText('标的代码'), '600000')
  await userEvent.click(screen.getByRole('button', { name: '发起获取' }))
  expect(await screen.findByText(/股票数据源未接入/)).toBeInTheDocument()
  expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/jobs/data')).toBe(false)
})

test('fetch report clears on edits and an old check cannot submit or restore it', async () => {
  let checks = 0
  let jobs = 0
  let finishCheck: ((response: Response) => void) | undefined
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets' || path === '/api/jobs') return Response.json({ items: [], next_cursor: null })
    if (path === '/api/capabilities/check') { checks += 1; if (checks === 2) return new Promise<Response>(resolve => { finishCheck = resolve }); return Response.json({ operation: 'fetch', status: 'ready', request_hash: 'h', dataset_version_id: null, input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null }) }
    if (path === '/api/jobs/data') { jobs += 1; return Response.json({ id: 'j1', kind: 'fetch', state: 'succeeded', result: { state: 'complete', market_prefetch_state: 'ready', dataset_version_id: 'd1' } }) }
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await screen.findByText(/尚无已验证数据版本/)
  await userEvent.type(screen.getByLabelText('标的代码'), '512480')
  await userEvent.click(screen.getByRole('button', { name: '发起获取' }))
  expect(await screen.findByText(/服务端检查：可执行/)).toBeInTheDocument()
  await userEvent.type(screen.getByLabelText('标的代码'), '0')
  expect(screen.queryByText(/服务端检查：可执行/)).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '发起获取' }))
  await waitFor(() => expect(checks).toBe(2))
  await userEvent.type(screen.getByLabelText('基准代码'), '1')
  await act(async () => { finishCheck!(Response.json({ operation: 'fetch', status: 'ready', request_hash: 'stale', dataset_version_id: null, input_hashes: {}, checks: [], assumptions: [], limitations: [], checker_version: 'v1', generated_at: null })); await Promise.resolve() })
  expect(screen.queryByText(/服务端检查：可执行/)).not.toBeInTheDocument()
  expect(jobs).toBe(1)
})
