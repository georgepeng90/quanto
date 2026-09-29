import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
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
  render(<MemoryRouter initialEntries={['/experiments']}><App /></MemoryRouter>)
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.clear(await screen.findByLabelText('初始资金'))
  await userEvent.type(screen.getByLabelText('初始资金'), '2000')
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  await waitFor(() => expect(screen.getByText(/保存冲突/)).toBeInTheDocument())
  expect(screen.getByLabelText('初始资金')).toHaveValue(2000)
  expect(screen.getAllByText(/服务端修订 1/).length).toBeGreaterThan(0)
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
  render(<MemoryRouter initialEntries={['/experiments']}><App /></MemoryRouter>)
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  await userEvent.clear(screen.getByLabelText('初始资金'))
  await userEvent.type(screen.getByLabelText('初始资金'), '2000')
  await userEvent.click(screen.getByRole('link', { name: '数据版本' }))
  expect(confirm).toHaveBeenCalled()
  expect(screen.getByLabelText('初始资金')).toHaveValue(2000)
  confirm.mockRestore()
})

test('does not save stale parameters while their JSON editor is invalid', async () => {
  render(<MemoryRouter initialEntries={['/experiments']}><App /></MemoryRouter>)
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  const editor = await screen.findByLabelText('参数 JSON（与表单共享状态）')
  await userEvent.clear(editor)
  fireEvent.change(editor, { target: { value: '{bad' } })
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  expect(screen.getByText(/请先修正参数或风险配置中的 JSON/)).toBeInTheDocument()
})

test('blocks dates outside the selected dataset price coverage', async () => {
  render(<MemoryRouter initialEntries={['/experiments']}><App /></MemoryRouter>)
  await userEvent.selectOptions(await screen.findByLabelText('实验草稿'), 'e1')
  fireEvent.change(await screen.findByLabelText('结束日'), { target: { value: '2025-01-01' } })
  await userEvent.click(screen.getByRole('button', { name: '保存实验' }))
  expect(await screen.findByText(/超出数据价格覆盖范围/)).toBeInTheDocument()
})

test('rejects a risk cap above the engine-supported range before save', async () => {
  render(<MemoryRouter initialEntries={['/experiments']}><App /></MemoryRouter>)
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
  render(<MemoryRouter initialEntries={['/experiments']}><App /></MemoryRouter>)
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
