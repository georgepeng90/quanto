import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import Ajv2020 from 'ajv/dist/2020.js'
import { api, describeError } from '../api'
import { stateLabel, terminalStates } from '../components/RunProgress'
import type { ExperimentDraft, RunRecord, RunState } from '../types'

type Item = { experiment_id: string; expected_revision: number }
type Prepared = { items: Item[]; key: string; batch: boolean }
type PreparingBatch = { signature: string; values: Record<string, unknown>[]; items: Item[]; key: string }
const merge = (old: RunRecord[], incoming: RunRecord[]) => {
  const records = new Map(old.map(run => [run.run_id, run]))
  for (const run of incoming) records.set(run.run_id, run)
  return [...records.values()]
}

export function RunsPage() {
  const [experiments, setExperiments] = useState<ExperimentDraft[]>([])
  const [experimentCursor, setExperimentCursor] = useState<string | null>(null)
  const [selected, setSelected] = useState('')
  const [parameters, setParameters] = useState('')
  const [runs, setRuns] = useState<RunRecord[]>([])
  const [runCursor, setRunCursor] = useState<string | null>(null)
  const [filter, setFilter] = useState<RunState | 'all'>('all')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState(false)
  const prepared = useRef<Prepared | null>(null)
  const preparingBatch = useRef<PreparingBatch | null>(null)
  const preparing = useRef(false)
  const runOffset = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([api.experiments(), api.runs(undefined, controller.signal)]).then(([drafts, page]) => {
      if (controller.signal.aborted) return
      setExperiments(drafts.items)
      setExperimentCursor(drafts.next_cursor)
      setRuns(page.items)
      setRunCursor(page.next_cursor)
      runOffset.current = page.items.length
    }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const active = runs.filter(run => !terminalStates.has(run.state))
    if (!active.length) return
    const controller = new AbortController()
    const timer = window.setInterval(async () => {
      try {
        const updated = await Promise.all(active.map(run => api.run(run.run_id, controller.signal)))
        if (!controller.signal.aborted) setRuns(old => merge(old, updated))
      } catch (value) { if (!controller.signal.aborted) setError(describeError(value)) }
    }, 1000)
    return () => { clearInterval(timer); controller.abort() }
  }, [runs.map(run => `${run.run_id}:${run.state}`).join('|')])

  const loadMoreExperiments = async () => {
    if (!experimentCursor) return
    try { const page = await api.experiments(experimentCursor); setExperiments(old => [...old, ...page.items.filter(item => !old.some(value => value.id === item.id))]); setExperimentCursor(page.next_cursor) }
    catch (value) { setError(describeError(value)) }
  }
  const loadMoreRuns = async () => {
    if (!runCursor) return
    try { const page = await api.runs(runCursor); setRuns(old => merge(old, page.items)); runOffset.current += page.items.length; setRunCursor(page.next_cursor) }
    catch (value) { setError(describeError(value)) }
  }
  const refresh = async () => {
    try {
      const [first, edge, tracked] = await Promise.all([
        api.runs(),
        runCursor === null && runOffset.current > 0 ? api.runs(String(runOffset.current)) : Promise.resolve(null),
        Promise.all(runs.filter(run => !terminalStates.has(run.state)).map(run => api.run(run.run_id)))
      ])
      setRuns(old => merge(old, [...first.items, ...(edge?.items || []), ...tracked]))
      setRunCursor(old => old ?? first.next_cursor ?? edge?.next_cursor ?? null)
      if (edge) runOffset.current += edge.items.length
      setError('')
    } catch (value) { setError(describeError(value)) }
  }

  const submitPrepared = async (request: Prepared) => {
    setBusy(true)
    setError('')
    try {
      const submitted = request.batch ? (await api.submitBatch(request.items, request.key)).items : [await api.submitRun(request.items[0].experiment_id, request.items[0].expected_revision, request.key)]
      setRuns(old => merge(old, submitted))
      setRetry(false)
    } catch (value) { setError(describeError(value)); setRetry(true) }
    finally { setBusy(false); preparing.current = false }
  }
  const submit = async (batch: boolean) => {
    if (preparing.current || busy) return
    preparing.current = true
    setError('')
    setRetry(false)
    prepared.current = null
    const draft = experiments.find(item => item.id === selected)
    if (!draft) { setError('请选择已保存的实验草稿'); preparing.current = false; return }
    let items: Item[] = [{ experiment_id: draft.id, expected_revision: draft.revision }]
    if (batch) {
      let values: unknown
      try { values = JSON.parse(parameters) } catch { setError('参数列表必须是 JSON 数组'); preparing.current = false; return }
      if (!Array.isArray(values) || values.length < 1 || values.length > 100) { setError('参数列表需包含 1 到 100 组，最多 100 组'); preparing.current = false; return }
      if (!values.every(value => value !== null && typeof value === 'object' && !Array.isArray(value))) { setError('每组参数必须是 JSON 对象'); preparing.current = false; return }
      try {
        const strategy = await api.strategy(draft.payload.strategy_revision_id)
        const schema = structuredClone(strategy.manifest.parameter_schema)
        delete (schema as typeof schema & { $schema?: string }).$schema
        const close = (node: typeof schema) => {
          if (node.type === 'object') { (node as typeof node & { additionalProperties: boolean }).additionalProperties = false; Object.values(node.properties || {}).forEach(close) }
          if (node.type === 'array' && node.items) close(node.items)
        }
        close(schema)
        const validate = new Ajv2020({ allErrors: true, useDefaults: true, strict: false }).compile(schema)
        for (let index = 0; index < values.length; index++) {
          if (!validate(structuredClone(values[index]))) { setError(`第 ${index + 1} 组参数无效：${validate.errors?.[0]?.message || '请检查字段'}`); preparing.current = false; return }
        }
      } catch (value) { setError(describeError(value)); preparing.current = false; return }
      try {
        const signature = JSON.stringify([draft.id, draft.revision, values])
        if (!preparingBatch.current || preparingBatch.current.signature !== signature) preparingBatch.current = { signature, values: values as Record<string, unknown>[], items: [], key: crypto.randomUUID() }
        const batchRequest = preparingBatch.current
        for (const value of batchRequest.values.slice(batchRequest.items.length)) {
          const variant = await api.createExperiment({ ...draft.payload, strategy_params: value as Record<string, unknown> })
          batchRequest.items.push({ experiment_id: variant.id, expected_revision: variant.revision })
        }
        items = [...batchRequest.items]
      } catch (value) { setError(describeError(value)); preparing.current = false; return }
    }
    const request = { items, key: batch ? preparingBatch.current!.key : crypto.randomUUID(), batch }
    preparingBatch.current = null
    prepared.current = request
    await submitPrepared(request)
  }
  const visible = runs.filter(run => filter === 'all' || run.state === filter)
  const groups = new Map<string, RunRecord[]>()
  for (const run of visible) { const key = run.group_id || run.run_id; groups.set(key, [...(groups.get(key) || []), run]) }
  return <div className="page-stack"><header className="page-heading"><h1>回测任务</h1><p>提交已保存实验的快照；同一参数列表按固定顺序组成一批任务。</p></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <section className="panel"><h2>发起运行</h2><div className="form-grid"><label>实验草稿<select value={selected} onChange={event => { setSelected(event.target.value); prepared.current = null; preparingBatch.current = null; setRetry(false) }}><option value="">选择实验</option>{experiments.map(item => <option key={item.id} value={item.id}>{item.id} · 修订 {item.revision}</option>)}</select></label><label className="wide">参数列表 JSON<textarea value={parameters} onChange={event => { setParameters(event.target.value); prepared.current = null; preparingBatch.current = null; setRetry(false) }} placeholder={'[{"window":4},{"window":8}]'} /></label></div>
      <div className="inline-actions"><button type="button" disabled={busy} onClick={() => submit(false)}>运行</button><button type="button" disabled={busy} onClick={() => submit(true)}>运行参数列表</button>{retry && <button type="button" disabled={busy} onClick={() => { if (prepared.current) void submitPrepared(prepared.current) }}>重试提交</button>}{experimentCursor && <button type="button" onClick={loadMoreExperiments}>加载更多实验草稿</button>}</div><p className="muted">参数列表最多 100 组。先逐组保存为独立实验草稿，再提交一个批次；重试沿用已准备的草稿和幂等键。</p></section>
    <section className="panel"><div className="section-heading"><div><h2>运行记录</h2><p>运行列表和状态来自服务端；队列中的任务也可查看详情。</p></div><button type="button" onClick={refresh}>刷新</button></div><label>状态筛选<select value={filter} onChange={event => setFilter(event.target.value as RunState | 'all')}><option value="all">全部</option>{Object.entries(stateLabel).map(([state, label]) => <option key={state} value={state}>{label}</option>)}</select></label>
      {visible.length === 0 ? <p className="empty">暂无匹配运行。</p> : [...groups].reverse().map(([group, items]) => <div key={group} className="run-group"><h3>{items[0].group_id ? `批次 ${group}` : '单次运行'}</h3><div className="record-list">{items.map(run => <article className="record" key={run.run_id}><div className="record-head"><Link to={run.detail_url}>{run.run_id}</Link><span className="badge">{stateLabel[run.state]}</span></div><p>实验 {run.experiment_id} · 修订 {run.experiment_revision} · 已完成至 {run.completed_through || '尚无完整交易日'}</p><small>提交 {run.created_at || '历史记录未知'}</small>{run.error && <p className="error">{run.error}</p>}</article>)}</div></div>)}{runCursor && <button type="button" onClick={loadMoreRuns}>加载更多运行</button>}</section>
  </div>
}
