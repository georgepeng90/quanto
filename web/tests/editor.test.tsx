import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { useState } from 'react'
import { App } from '../src/App'
import { ParameterForm } from '../src/components/ParameterForm'
import { StrategyEditor } from '../src/components/StrategyEditor'
import { EditorView } from '@codemirror/view'
import type { ParameterSchema } from '../src/types'

const manifest = { entrypoint: 'strategy.py:create_strategy', parameter_schema: { type: 'object', properties: { window: { type: 'integer', title: '窗口', default: 4 } } }, auxiliary_sources: ['helper.py'], research_data: [] }
const strategy = { id: 's1', content_hash: 'hash', manifest, files: { 'strategy.json': JSON.stringify(manifest), 'strategy.py': 'from helper import value\ndef create_strategy(params): return value', 'helper.py': 'value = 1' } }
const dataset = { id: 'd1', content_hash: 'hash', manifest: { source: 'sample', is_demo: true, price_mode: 'adjusted_research', calendar_start: '2024-01-01', calendar_end: '2024-12-31', data_start: '2024-02-01', data_end: '2024-11-30', event_coverage: 'unknown', limitations: ['示例'] } }
const spec = { start: '2024-02-01', end: '2024-03-01', symbols: ['000001'], initial_cash: 1000, seed: 7, execution: { commission_rate: 0, minimum_commission: 0, slippage_rate: 0, max_stale_sessions: 0, model: 'next_open' }, risk: { max_total_weight: 1, max_symbol_weight: 1, group_caps: {}, stress_scenarios: [], pause_action: 'block_new', recovery_mode: 'never' }, metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: 'v1' } }
const experiment = { id: 'e1', revision: 1, payload: { strategy_revision_id: 's1', dataset_version_id: 'd1', strategy_params: { window: 4 }, backtest_spec: spec, sample_purpose: 'exploration' } }
const renderApp = () => render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments'] })} />)

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [dataset], next_cursor: null })
    if (path === '/api/strategies') return Response.json({ items: [strategy], next_cursor: null })
    if (path === '/api/experiments') return Response.json({ items: [experiment], next_cursor: null })
    if (path === '/api/strategies/s1') return Response.json(strategy)
    if (path === '/api/experiments/e1' && init?.method === 'PUT') return Response.json({ code: 'CONFLICT', message: '修订或幂等键冲突', details: {} }, { status: 409 })
    if (path === '/api/experiments/e1') return Response.json(experiment)
    if (path === '/api/datasets/d1') return Response.json(dataset)
    throw new Error(`unexpected ${init?.method || 'GET'} ${path}`)
  }))
})

test('renders a newly declared parameter and edits the shared value', async () => {
  const onChange = vi.fn()
  const schema: ParameterSchema = { type: 'object', properties: { window: { type: 'integer', title: '窗口', default: 4 }, evaluation: { type: 'string', enum: ['daily', 'weekly'] } } }
  function Harness() { const [value, setValue] = useState<Record<string, unknown>>({ window: 4 }); return <ParameterForm schema={schema} value={value} onChange={next => { setValue(next); onChange(next) }} /> }
  render(<Harness />)
  await userEvent.clear(screen.getByRole('spinbutton', { name: '窗口' }))
  await userEvent.type(screen.getByRole('spinbutton', { name: '窗口' }), '8')
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ window: 8 }))
  expect(screen.getByLabelText('evaluation')).toBeInTheDocument()
})

test('preserves unsaved experiment edits on revision conflict and shows server revision', async () => {
  const priorFetch = globalThis.fetch
  let reads = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/experiments/e1' && !init?.method) {
      reads += 1
      return Response.json(reads === 1 ? experiment : { ...experiment, revision: 2, payload: { ...experiment.payload, sample_purpose: 'server-only' } })
    }
    return priorFetch(input, init)
  }))
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.clear(await screen.findByLabelText('初始资金'))
  await userEvent.type(screen.getByLabelText('初始资金'), '2000')
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  await waitFor(() => expect(screen.getByText(/保存冲突/)).toBeInTheDocument())
  expect(screen.getByLabelText('初始资金')).toHaveValue(2000)
  expect(screen.getAllByText(/服务端修订 2/).length).toBeGreaterThan(0)
  expect(screen.getByLabelText('服务端实验内容')).toHaveTextContent('server-only')
  expect(screen.getByLabelText('样本用途')).toHaveValue('exploration')
  fireEvent.change(screen.getByLabelText('初始资金'), { target: { value: '3000' } })
  expect(screen.getByLabelText('服务端实验内容')).toHaveTextContent('server-only')
})

test('shows source file and line for server AST syntax errors', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/strategies') return Response.json({ code: 'SYNTAX_ERROR', message: '策略代码语法有误', details: { file: 'helper.py', line: 3, offset: 1 } }, { status: 422 })
    throw new Error(`unexpected ${input}`)
  }))
  render(<StrategyEditor revision={strategy} onSaved={vi.fn()} onDirtyChange={vi.fn()} />)
  await userEvent.click(screen.getByRole('button', { name: '保存新修订' }))
  expect(await screen.findByText(/helper.py:3/)).toBeInTheDocument()
})

test('registers edited auxiliary source with the complete manifest', async () => {
  let submitted: { manifest: unknown; files: Record<string, string> } | null = null
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (String(input) === '/api/strategies' && init?.method === 'POST') { submitted = JSON.parse(String(init.body)); return Response.json({ id: 's2', content_hash: 'newhash', manifest }) }
    throw new Error(`unexpected ${input}`)
  }))
  render(<StrategyEditor revision={strategy} onSaved={vi.fn()} onDirtyChange={vi.fn()} />)
  await userEvent.click(screen.getByRole('button', { name: 'helper.py' }))
  const editor = document.querySelector('.cm-editor') as HTMLElement
  const view = EditorView.findFromDOM(editor)!
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: 'value = 2' } })
  await userEvent.click(screen.getByRole('button', { name: '保存新修订' }))
  await waitFor(() => expect(submitted).not.toBeNull())
  expect(submitted!.files['helper.py']).toBe('value = 2')
  expect(submitted!.files['strategy.py']).toContain('create_strategy')
  expect(JSON.parse(submitted!.files['strategy.json'])).toEqual(submitted!.manifest)
})

test('declares a newly added Python source in the registered manifest', async () => {
  let submitted: { manifest: { auxiliary_sources: string[] }; files: Record<string, string> } | null = null
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (String(input) === '/api/strategies' && init?.method === 'POST') { submitted = JSON.parse(String(init.body)); return Response.json({ id: 's2', content_hash: 'newhash', manifest: submitted!.manifest }) }
    throw new Error(`unexpected ${input}`)
  }))
  render(<StrategyEditor revision={strategy} onSaved={vi.fn()} onDirtyChange={vi.fn()} />)
  await userEvent.type(screen.getByLabelText('新增源码文件'), 'filter.py')
  await userEvent.click(screen.getByRole('button', { name: '添加文件' }))
  await userEvent.click(screen.getByRole('button', { name: '保存新修订' }))
  await waitFor(() => expect(submitted).not.toBeNull())
  expect(submitted!.manifest.auxiliary_sources).toContain('filter.py')
  expect(submitted!.files).toHaveProperty('filter.py')
})

test('asks before leaving a dirty experiment form', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.clear(screen.getByLabelText('初始资金'))
  await userEvent.type(screen.getByLabelText('初始资金'), '2000')
  await userEvent.click(screen.getByRole('link', { name: '数据版本' }))
  expect(confirm).toHaveBeenCalled()
  expect(screen.getByLabelText('初始资金')).toHaveValue(2000)
  confirm.mockRestore()
})

test('blocks browser back and forward until dirty edits are explicitly discarded', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const router = createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/datasets', '/experiments', '/runs'], initialIndex: 1 })
  render(<RouterProvider router={router} />)
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.clear(screen.getByLabelText('初始资金'))
  await userEvent.type(screen.getByLabelText('初始资金'), '2000')
  await act(async () => { await router.navigate(-1) })
  await waitFor(() => expect(confirm).toHaveBeenCalled())
  expect(router.state.location.pathname).toBe('/experiments')
  expect(screen.getByLabelText('初始资金')).toHaveValue(2000)
  await act(async () => { await router.navigate(1) })
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(2))
  expect(router.state.location.pathname).toBe('/experiments')
  confirm.mockReturnValue(true)
  await act(async () => { await router.navigate(-1) })
  await waitFor(() => expect(router.state.location.pathname).toBe('/datasets'))
  confirm.mockRestore()
})

test('does not save stale parameters while their JSON editor is invalid', async () => {
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  const editor = await screen.findByLabelText('参数 JSON（与表单共享状态）')
  await userEvent.clear(editor)
  fireEvent.change(editor, { target: { value: '{bad' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: '窗口' }), { target: { value: '8' } })
  expect(editor).toHaveValue('{bad')
  expect(screen.getByRole('button', { name: '验证当前策略与参数' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  expect(screen.getByText(/请先修正参数或风险配置中的 JSON/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '放弃未完成的参数 JSON' }))
  expect((editor as HTMLTextAreaElement).value).toContain('"window": 8')
})

test('does not attach in-flight or completed validation to edited parameters and translates queued state', async () => {
  let release: ((value: Response) => void) | undefined
  const priorFetch = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/jobs/strategy-validation' && init?.method === 'POST') return new Promise<Response>(resolve => { release = resolve })
    return priorFetch(input, init)
  }))
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.click(screen.getByRole('button', { name: '验证当前策略与参数' }))
  await waitFor(() => expect(release).toBeDefined())
  fireEvent.change(screen.getByRole('spinbutton', { name: '窗口' }), { target: { value: '8' } })
  release!(Response.json({ id: 'v1', kind: 'strategy-validation', state: 'succeeded', result: { valid: true, warmup_bars: 4 } }))
  await waitFor(() => expect(screen.queryByText(/策略协议有效/)).not.toBeInTheDocument())
  const fetchAfterEdit = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/jobs/strategy-validation' && init?.method === 'POST') return Response.json({ id: 'v2', kind: 'strategy-validation', state: 'queued' })
    return fetchAfterEdit(input, init)
  }))
  await userEvent.click(screen.getByRole('button', { name: '验证当前策略与参数' }))
  expect(await screen.findByText('验证任务 排队中')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('spinbutton', { name: '窗口' }), { target: { value: '9' } })
  expect(screen.queryByText('验证任务 排队中')).not.toBeInTheDocument()
})

test('removes a completed valid result when its strategy parameters change', async () => {
  const priorFetch = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/jobs/strategy-validation' && init?.method === 'POST') return Response.json({ id: 'v3', kind: 'strategy-validation', state: 'succeeded', result: { valid: true, warmup_bars: 4 } })
    return priorFetch(input, init)
  }))
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.click(screen.getByRole('button', { name: '验证当前策略与参数' }))
  expect(await screen.findByText(/策略协议有效 · 解析预热 4 根/)).toBeInTheDocument()
  fireEvent.change(screen.getByRole('spinbutton', { name: '窗口' }), { target: { value: '8' } })
  expect(screen.queryByText(/策略协议有效/)).not.toBeInTheDocument()
})

test('blocks dates outside the selected dataset price coverage', async () => {
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(await screen.findByLabelText('结束日'), { target: { value: '2025-01-01' } })
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  expect(await screen.findByText(/超出数据价格覆盖范围/)).toBeInTheDocument()
})

test('rejects a risk cap above the engine-supported range before save', async () => {
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(await screen.findByLabelText('总仓位上限'), { target: { value: '1.5' } })
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  expect(await screen.findByText(/总仓位上限必须在 0 到 1 之间/)).toBeInTheDocument()
})

test('saves a new draft revision with edited schema parameters and complete spec', async () => {
  let submitted: { expected_revision: number; payload: typeof experiment.payload } | null = null
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [dataset], next_cursor: null })
    if (path === '/api/strategies') return Response.json({ items: [strategy], next_cursor: null })
    if (path === '/api/experiments') return Response.json({ items: [experiment], next_cursor: null })
    if (path === '/api/strategies/s1') return Response.json(strategy)
    if (path === '/api/datasets/d1') return Response.json(dataset)
    if (path === '/api/experiments/e1' && init?.method === 'PUT') { submitted = JSON.parse(String(init.body)); return Response.json({ id: 'e1', revision: 2, payload: submitted!.payload }) }
    if (path === '/api/experiments/e1') return Response.json(experiment)
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  const field = await screen.findByRole('spinbutton', { name: '窗口' })
  await userEvent.clear(field)
  await userEvent.type(field, '6')
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  await waitFor(() => expect(submitted).not.toBeNull())
  expect(submitted!.expected_revision).toBe(1)
  expect(submitted!.payload.strategy_params).toEqual({ window: 6 })
  expect(submitted!.payload.backtest_spec.risk.pause_action).toBe('block_new')
  expect(submitted!.payload.backtest_spec.execution.model).toBe('next_open')
})

test('loads later dataset, strategy, and experiment pages into their selectors', async () => {
  const laterDataset = { ...dataset, id: 'd2', manifest: { ...dataset.manifest, source: 'later-dataset' } }
  const laterStrategy = { ...strategy, id: 's2', manifest: { ...manifest, name: 'later-strategy' } }
  const laterExperiment = { ...experiment, id: 'e2' }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api', timezone: 'Asia/Shanghai' })
    if (path === '/api/datasets') return Response.json({ items: [dataset], next_cursor: '50' })
    if (path === '/api/strategies') return Response.json({ items: [strategy], next_cursor: '50' })
    if (path === '/api/experiments') return Response.json({ items: [experiment], next_cursor: '50' })
    if (path === '/api/datasets?cursor=50') return Response.json({ items: [laterDataset], next_cursor: null })
    if (path === '/api/strategies?cursor=50') return Response.json({ items: [laterStrategy], next_cursor: null })
    if (path === '/api/experiments?cursor=50') return Response.json({ items: [laterExperiment], next_cursor: null })
    throw new Error(`unexpected ${path}`)
  }))
  renderApp()
  await userEvent.click(await screen.findByRole('button', { name: '加载更多数据版本' }))
  await userEvent.click(screen.getByRole('button', { name: '加载更多策略修订' }))
  await userEvent.click(screen.getByRole('button', { name: '加载更多实验草稿' }))
  expect(await screen.findByRole('option', { name: /later-dataset/ })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: /later-strategy/ })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: /e2/ })).toBeInTheDocument()
})


test('enum choices preserve typed values and empty string distinct from unset', async () => {
  function Harness() { const [value, setValue] = useState<Record<string, unknown>>({}); return <ParameterForm schema={{ type: 'object', properties: { count: { type: 'integer', enum: [1, 2] }, enabled: { type: 'boolean', enum: [false, true] }, mode: { type: 'string', enum: ['', 'x'] } } }} value={value} onChange={setValue} /> }
  render(<Harness />)
  for (const [name, label] of [['count', '2'], ['enabled', 'true'], ['mode', '']]) {
    const select = screen.getByLabelText(name) as HTMLSelectElement
    const option = [...select.options].find(item => item.text === label)!
    await userEvent.selectOptions(select, option)
  }
  expect(JSON.parse((screen.getByLabelText('参数 JSON（与表单共享状态）') as HTMLTextAreaElement).value)).toEqual({ count: 2, enabled: true, mode: '' })
  await userEvent.selectOptions(screen.getByLabelText('mode'), '')
  expect(JSON.parse((screen.getByLabelText('参数 JSON（与表单共享状态）') as HTMLTextAreaElement).value)).toEqual({ count: 2, enabled: true })
})

test.each([false, true])('late experiment save preserves newer typing or switched identity (%s)', async switched => {
  let release!: (value: Response) => void
  let requestBody: typeof experiment | undefined
  const previous = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/experiments/e1' && init?.method === 'PUT') { requestBody = JSON.parse(String(init.body)); return new Promise<Response>(resolve => { release = resolve }) }
    return previous(input, init)
  }))
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  renderApp()
  await screen.findByRole('option', { name: /e1/ })
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await screen.findByText(/当前修订 s1/)
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  await waitFor(() => expect(requestBody).toBeDefined())
  if (switched) await userEvent.selectOptions(screen.getByLabelText('实验草稿'), '')
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: 'typed after save' } })
  await act(async () => release(Response.json({ ...experiment, revision: 2 })))
  expect(screen.getByLabelText('样本用途')).toHaveValue('typed after save')
  expect(screen.getByText('实验配置未保存')).toBeInTheDocument()
  expect(screen.getByLabelText('实验草稿')).toHaveValue(switched ? '' : 'e1')
  if (!switched) {
    expect(screen.getByText('修订 2', { exact: true })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
    await waitFor(() => expect(requestBody).toEqual(expect.objectContaining({ expected_revision: 2, payload: expect.objectContaining({ sample_purpose: 'typed after save' }) })))
    await act(async () => release(Response.json({ ...experiment, revision: 3, payload: { ...experiment.payload, sample_purpose: 'typed after save' } })))
  }
})

test.each([false, true])('late source registration preserves newer buffer or switched revision (%s)', async switched => {
  let release!: (value: Response) => void
  const onSaved = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/session') return Response.json({ token: 'token' })
    return new Promise<Response>(resolve => { release = resolve })
  }))
  const props = { onSaved, onDirtyChange: vi.fn() }
  const view = render(<StrategyEditor revision={strategy} {...props} />)
  await userEvent.click(screen.getByRole('button', { name: '保存新修订' }))
  await waitFor(() => expect(release).toBeDefined())
  if (switched) view.rerender(<StrategyEditor revision={{ ...strategy, id: 's3', files: { ...strategy.files, 'strategy.py': 'switched source' } }} {...props} />)
  const editor = EditorView.findFromDOM(document.querySelector('.cm-editor') as HTMLElement)!
  act(() => editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: 'newer source' } }))
  await act(async () => release(Response.json({ ...strategy, id: 's2' })))
  expect(editor.state.doc.toString()).toBe('newer source')
  expect(screen.getByText('未保存', { exact: true })).toBeInTheDocument()
  expect(onSaved).not.toHaveBeenCalled()
})


test('historical draft deep link remains selected beyond the first listing page', async () => {
  const previous = globalThis.fetch
  const historical = { ...experiment, id: 'historic-clone', payload: { ...experiment.payload, source_run_id: 'old-run' } }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/experiments/historic-clone') return Response.json(historical)
    return previous(input, init)
  }))
  render(<RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: ['/experiments?draft=historic-clone'] })} />)
  await screen.findByText(/当前修订 s1/)
  expect(screen.getByLabelText('实验草稿')).toHaveValue('historic-clone')
  expect(screen.getByLabelText('样本用途')).toHaveValue('exploration')
})


test('switching source files during the typing latch binds the buffer to its file', async () => {
  render(<StrategyEditor revision={strategy} onSaved={vi.fn()} onDirtyChange={vi.fn()} />)
  const pythonView = EditorView.findFromDOM(document.querySelector('.cm-editor') as HTMLElement)!
  act(() => pythonView.dispatch({ changes: { from: 0, to: pythonView.state.doc.length, insert: 'from quanto import something' } }))
  await userEvent.click(screen.getByRole('button', { name: 'strategy.json' }))
  const jsonView = EditorView.findFromDOM(document.querySelector('.cm-editor') as HTMLElement)!
  expect(jsonView.state.doc.toString()).toBe(JSON.stringify(manifest))
  act(() => jsonView.dispatch({ changes: { from: 0, to: jsonView.state.doc.length, insert: JSON.stringify({ ...manifest, name: 'new declaration' }) } }))
  await userEvent.click(screen.getByRole('button', { name: 'strategy.py' }))
  expect(EditorView.findFromDOM(document.querySelector('.cm-editor') as HTMLElement)!.state.doc.toString()).toBe('from quanto import something')
})


test('current draft cloning preserves dirty source until saved and still clones unsaved configuration', async () => {
  const previous = globalThis.fetch
  let savedFiles: Record<string, string> | undefined
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === '/api/strategies' && init?.method === 'POST') {
      savedFiles = JSON.parse(String(init.body)).files
      return Response.json({ ...strategy, id: 's2', files: savedFiles })
    }
    if (String(input) === '/api/strategies/s2') return Response.json({ ...strategy, id: 's2', files: savedFiles })
    return previous(input, init)
  }))
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  renderApp()
  await screen.findByRole('option', { name: /e1/ })
  await userEvent.selectOptions(screen.getByLabelText('实验草稿'), 'e1')
  await screen.findByText(/当前修订 s1/)
  const edited = '# unsaved source changes\n' + strategy.files['strategy.py']
  const source = () => EditorView.findFromDOM(document.querySelector('.cm-editor') as HTMLElement)!
  act(() => source().dispatch({ changes: { from: 0, to: source().state.doc.length, insert: edited } }))
  const clone = screen.getByRole('button', { name: '克隆为新实验' })
  await userEvent.click(clone)
  expect(source().state.doc.toString()).toBe(edited)
  expect(clone).toBeDisabled()
  expect(screen.getByText('未保存', { exact: true })).toBeVisible()
  expect(screen.getByText(/请先保存策略新修订，或切换草稿并确认放弃源码修改，再克隆/)).toBeVisible()
  expect(screen.getByLabelText('实验草稿')).toHaveValue('e1')
  await userEvent.click(screen.getByRole('button', { name: '保存新修订' }))
  await waitFor(() => expect(savedFiles?.['strategy.py']).toBe(edited))
  await waitFor(() => expect(clone).toBeEnabled())
  fireEvent.change(screen.getByLabelText('样本用途'), { target: { value: 'unsaved configuration to clone' } })
  await userEvent.click(clone)
  expect(screen.getByLabelText('实验草稿')).toHaveValue('')
  expect(screen.getByLabelText('样本用途')).toHaveValue('unsaved configuration to clone')
  expect(source().state.doc.toString()).toBe(edited)
})
