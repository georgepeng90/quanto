import { useEffect, useRef, useState } from 'react'
import Ajv2020 from 'ajv/dist/2020.js'
import { Link, useSearchParams } from 'react-router-dom'
import { BacktestCatalog } from '../components/BacktestCatalog'
import { ModelBacktestForm } from '../components/ModelBacktestForm'
import { api, describeError } from '../api'
import { terminalStates } from '../components/RunProgress'
import type { BacktestCatalogRow, ExperimentDraft } from '../types'

type Item = { experiment_id: string; expected_revision: number }
type Prepared = { items: Item[]; key: string; batch: boolean }
type PreparingBatch = { signature: string; values: Record<string, unknown>[]; items: Item[]; key: string }

export function RunsPage() {
  const [searchParams] = useSearchParams()
  const initialModel = searchParams.get('model') || ''
  const initialVersion = searchParams.get('version') || ''
  const [creating, setCreating] = useState(!!initialModel)
  const [source, setSource] = useState(initialModel ? 'model' : 'fixed')
  useEffect(() => { if (initialModel) { setCreating(true); setSource('model') } }, [initialModel, initialVersion])
  const [experiments, setExperiments] = useState<ExperimentDraft[]>([])
  const [experimentCursor, setExperimentCursor] = useState<string | null>(null)
  const [selected, setSelected] = useState('')
  const [parameters, setParameters] = useState('')
  const [catalog, setCatalog] = useState<BacktestCatalogRow[]>([])
  const [listComplete, setListComplete] = useState(false)
  const [listBusy, setListBusy] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState(false)
  const prepared = useRef<Prepared | null>(null)
  const preparingBatch = useRef<PreparingBatch | null>(null)
  const preparing = useRef(false)
  const inputGeneration = useRef(0)
  const loadGeneration = useRef(0)
  const mountController = useRef<AbortController | null>(null)

  const loadCatalog = async (signal?: AbortSignal) => {
    const generation = ++loadGeneration.current
    setListBusy(true)
    let cursor: string | undefined
    const rows: BacktestCatalogRow[] = []
    const seen = new Set<string>()
    try {
      for (;;) {
        const page = await api.backtestCatalog(cursor, signal)
        if (signal?.aborted || generation !== loadGeneration.current) return
        for (const row of page.items) if (!seen.has(row.id)) { rows.push(row); seen.add(row.id) }
        if (!page.next_cursor) break
        if (page.next_cursor === cursor) throw new Error('Catalog cursor did not advance')
        cursor = page.next_cursor
      }
      if (signal?.aborted || generation !== loadGeneration.current) return
      setCatalog(rows)
      setListComplete(true)
      setError('')
    } catch (value) { if (!signal?.aborted && generation === loadGeneration.current) setError(describeError(value)) }
    finally { if (!signal?.aborted && generation === loadGeneration.current) setListBusy(false) }
  }

  useEffect(() => {
    const controller = new AbortController()
    mountController.current = controller
    void loadCatalog(controller.signal)
    api.experiments().then(drafts => {
      if (controller.signal.aborted) return
      setExperiments(drafts.items)
      setExperimentCursor(drafts.next_cursor)
    }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => { controller.abort(); mountController.current = null }
  }, [])

  useEffect(() => {
    if (!listComplete || !catalog.some(row => !terminalStates.has(row.state))) return
    const timer = window.setInterval(() => { void loadCatalog(mountController.current?.signal) }, 3000)
    return () => clearInterval(timer)
  }, [catalog, listComplete])

  const loadMoreExperiments = async () => {
    if (!experimentCursor) return
    try { const page = await api.experiments(experimentCursor); setExperiments(old => [...old, ...page.items.filter(item => !old.some(value => value.id === item.id))]); setExperimentCursor(page.next_cursor) }
    catch (value) { setError(describeError(value)) }
  }
  const sendPrepared = async (request: Prepared) => {
    setError('')
    try {
      request.batch ? await api.submitBatch(request.items, request.key) : await api.submitRun(request.items[0].experiment_id, request.items[0].expected_revision, request.key)
      void loadCatalog(mountController.current?.signal)
      if (prepared.current === request) setRetry(false)
    } catch (value) { setError(describeError(value)); if (prepared.current === request) setRetry(true) }
  }
  const submit = async (batch: boolean) => {
    if (preparing.current || busy) return
    preparing.current = true
    setBusy(true)
    const generation = inputGeneration.current
    try {
      setError('')
      setRetry(false)
      prepared.current = null
      const draft = experiments.find(item => item.id === selected)
      if (!draft) { setError('请选择已保存的实验草稿'); return }
      let items: Item[] = [{ experiment_id: draft.id, expected_revision: draft.revision }]
      let batchRequest: PreparingBatch | null = null
      if (batch) {
        let values: unknown
        try { values = JSON.parse(parameters) } catch { setError('参数列表必须是 JSON 数组'); return }
        if (!Array.isArray(values) || values.length < 1 || values.length > 100) { setError('参数列表需包含 1 到 100 组，最多 100 组'); return }
        if (!values.every(value => value !== null && typeof value === 'object' && !Array.isArray(value))) { setError('每组参数必须是 JSON 对象'); return }
        const strategy = await api.strategy(draft.payload.strategy_revision_id)
        if (generation !== inputGeneration.current) return
        const schema = structuredClone(strategy.manifest.parameter_schema)
        delete (schema as typeof schema & { $schema?: string }).$schema
        const close = (node: typeof schema) => {
          if (node.type === 'object') { (node as typeof node & { additionalProperties: boolean }).additionalProperties = false; Object.values(node.properties || {}).forEach(close) }
          if (node.type === 'array' && node.items) close(node.items)
        }
        close(schema)
        const validate = new Ajv2020({ allErrors: true, useDefaults: true, strict: false }).compile(schema)
        for (let index = 0; index < values.length; index++) {
          if (!validate(structuredClone(values[index]))) { setError(`第 ${index + 1} 组参数无效：${validate.errors?.[0]?.message || '请检查字段'}`); return }
        }
        const signature = JSON.stringify([draft.id, draft.revision, values])
        if (!preparingBatch.current || preparingBatch.current.signature !== signature) preparingBatch.current = { signature, values: values as Record<string, unknown>[], items: [], key: crypto.randomUUID() }
        batchRequest = preparingBatch.current
        for (const value of batchRequest.values.slice(batchRequest.items.length)) {
          const variant = await api.createExperiment({ ...draft.payload, strategy_params: value as Record<string, unknown> })
          if (generation !== inputGeneration.current) return
          batchRequest.items.push({ experiment_id: variant.id, expected_revision: variant.revision })
        }
        items = [...batchRequest.items]
      }
      if (generation !== inputGeneration.current) return
      const request = { items, key: batchRequest?.key || crypto.randomUUID(), batch }
      preparingBatch.current = null
      prepared.current = request
      await sendPrepared(request)
    } catch (value) { if (generation === inputGeneration.current) setError(describeError(value)) }
    finally { preparing.current = false; setBusy(false) }
  }
  const retryPrepared = async () => {
    const request = prepared.current
    if (!request || preparing.current) return
    preparing.current = true
    setBusy(true)
    try { await sendPrepared(request) }
    finally { preparing.current = false; setBusy(false) }
  }
  return <div className="page-stack runs-page"><header className="page-heading section-heading"><div><h1>回测</h1><p>查看全部历史报告、比较冻结条件，或使用固定策略实验与模型保存版本发起回测。</p></div><button onClick={() => setCreating(value => !value)} aria-expanded={creating}>{creating ? '收起新建回测' : '新建回测'}</button></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <BacktestCatalog rows={catalog} complete={listComplete} busy={listBusy} refresh={() => void loadCatalog(mountController.current?.signal)} />
    <Link to="/compare">固定策略曲线对比</Link>
    {creating && <><section className="panel"><h2>新建回测</h2><label>回测来源<select value={source} onChange={event => setSource(event.target.value)}><option value="fixed">固定策略实验</option><option value="model">强化学习保存版本</option></select></label></section>
    {source === 'model' ? <ModelBacktestForm key={`${initialModel}:${initialVersion}`} initialModel={initialModel} initialVersion={initialVersion} onSubmitted={() => void loadCatalog(mountController.current?.signal)} /> : <section className="panel"><h2>发起固定策略回测</h2><div className="form-grid"><label>实验草稿<select value={selected} onChange={event => { inputGeneration.current++; setSelected(event.target.value); prepared.current = null; preparingBatch.current = null; setRetry(false) }}><option value="">选择实验</option>{experiments.map(item => <option key={item.id} value={item.id}>{item.id} · 修订 {item.revision}</option>)}</select></label><label className="wide">参数列表 JSON<textarea value={parameters} onChange={event => { inputGeneration.current++; setParameters(event.target.value); prepared.current = null; preparingBatch.current = null; setRetry(false) }} placeholder={'[{"window":4},{"window":8}]'} /></label></div>
      <div className="inline-actions"><button type="button" disabled={busy} onClick={() => submit(false)}>运行</button><button type="button" disabled={busy} onClick={() => submit(true)}>运行参数列表</button>{retry && <button type="button" disabled={busy} onClick={retryPrepared}>重试提交</button>}{experimentCursor && <button type="button" onClick={loadMoreExperiments}>加载更多实验草稿</button>}</div><p className="muted">参数列表最多 100 组。先逐组保存为独立实验草稿，再提交一个批次；重试沿用已准备的草稿和幂等键。</p></section>
    }</>}
  </div>
}
