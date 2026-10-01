import { useEffect, useRef, useState } from 'react'
import Ajv2020 from 'ajv/dist/2020.js'
import { RunSummary } from '../components/RunSummary'
import { api, describeError } from '../api'
import { stateLabel, terminalStates } from '../components/RunProgress'
import type { ExperimentDraft, RunRecord, RunResult, RunState } from '../types'

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
  const [listComplete, setListComplete] = useState(false)
  const [listBusy, setListBusy] = useState(false)
  const [filter, setFilter] = useState<RunState | 'all'>('all')
  const [groupFilter, setGroupFilter] = useState('all')
  const [summaries, setSummaries] = useState<Record<string, { provenance?: Record<string, unknown>; result?: RunResult }>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState(false)
  const prepared = useRef<Prepared | null>(null)
  const preparingBatch = useRef<PreparingBatch | null>(null)
  const preparing = useRef(false)
  const inputGeneration = useRef(0)
  const runOffset = useRef(0)
  const runCursorRef = useRef<string | null>(null)
  const draining = useRef(false)
  const refreshing = useRef(false)
  const summaryCache = useRef<Record<string, { provenance?: Record<string, unknown>; result?: RunResult }>>({})
  const summaryLoading = useRef(new Set<string>())
  const mountController = useRef<AbortController | null>(null)

  const drainRuns = async (start?: string, signal?: AbortSignal) => {
    if (draining.current) return
    draining.current = true
    setListBusy(true)
    setListComplete(false)
    let cursor = start
    try {
      for (;;) {
        const page = await api.runs(cursor, signal)
        if (signal?.aborted) return
        setRuns(old => merge(old, page.items))
        runOffset.current = Math.max(runOffset.current, Number(cursor || '0') + page.items.length)
        runCursorRef.current = page.next_cursor
        if (!page.next_cursor) { setListComplete(true); setError(''); return }
        if (page.next_cursor === cursor) throw new Error('运行列表游标未前进')
        cursor = page.next_cursor
      }
    } catch (value) { if (!signal?.aborted) setError(describeError(value)) }
    finally { draining.current = false; if (!signal?.aborted) setListBusy(false) }
  }

  useEffect(() => {
    const controller = new AbortController()
    mountController.current = controller
    void drainRuns(undefined, controller.signal)
    api.experiments().then(drafts => {
      if (controller.signal.aborted) return
      setExperiments(drafts.items)
      setExperimentCursor(drafts.next_cursor)
    }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => { controller.abort(); mountController.current = null }
  }, [])

  useEffect(() => {
    if (!listComplete) return
    const signal = mountController.current?.signal
    for (const run of runs) {
      const cached = summaryCache.current[run.run_id]
      if ((cached?.provenance && (run.state !== 'succeeded' || cached.result)) || summaryLoading.current.has(run.run_id)) continue
      summaryLoading.current.add(run.run_id)
      void (async () => {
        try {
          const provenance = cached?.provenance || (await api.run(run.run_id, signal)).provenance
          const result = run.state === 'succeeded' ? await api.runResult(run.run_id, signal) : cached?.result
          if (signal?.aborted) return
          const summary = { provenance, result }
          summaryCache.current[run.run_id] = summary
          setSummaries(old => ({ ...old, [run.run_id]: summary }))
        } catch (value) { if (!signal?.aborted) setError(describeError(value)) }
        finally { summaryLoading.current.delete(run.run_id) }
      })()
    }
  }, [runs, listComplete])

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
    await drainRuns(runCursorRef.current || undefined, mountController.current?.signal)
  }
  const refresh = async () => {
    if (draining.current || refreshing.current) return
    refreshing.current = true
    setListBusy(true)
    const signal = mountController.current?.signal
    try {
      const edgeOffset = runOffset.current
      const cursor = runCursorRef.current
      const [first, edge, tracked] = await Promise.all([
        api.runs(undefined, signal),
        cursor === null && edgeOffset > 0 ? api.runs(String(edgeOffset), signal) : Promise.resolve(null),
        Promise.all(runs.filter(run => !terminalStates.has(run.state)).map(run => api.run(run.run_id, signal)))
      ])
      if (signal?.aborted) return
      setRuns(old => merge(old, [...first.items, ...(edge?.items || []), ...tracked]))
      if (edge) runOffset.current = Math.max(runOffset.current, edgeOffset + edge.items.length)
      else if (cursor === null) runOffset.current = Math.max(runOffset.current, first.items.length)
      const continuation = cursor || (edge ? edge.next_cursor : first.next_cursor)
      runCursorRef.current = continuation
      setError('')
      if (continuation) await drainRuns(continuation, signal)
      else setListComplete(true)
    } catch (value) { if (!signal?.aborted) setError(describeError(value)) }
    finally { refreshing.current = false; if (!signal?.aborted) setListBusy(false) }
  }

  const sendPrepared = async (request: Prepared) => {
    setError('')
    try {
      const submitted = request.batch ? (await api.submitBatch(request.items, request.key)).items : [await api.submitRun(request.items[0].experiment_id, request.items[0].expected_revision, request.key)]
      setRuns(old => merge(old, submitted))
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
  const visible = listComplete ? runs.filter(run => (filter === 'all' || run.state === filter) && (groupFilter === 'all' || (run.group_id || 'single') === groupFilter)) : []
  const groupIds = [...new Set(runs.map(run => run.group_id || 'single'))]
  const groups = new Map<string, RunRecord[]>()
  for (const run of visible) { const key = run.group_id || run.run_id; groups.set(key, [...(groups.get(key) || []), run]) }
  return <div className="page-stack"><header className="page-heading"><h1>回测任务</h1><p>提交已保存实验的快照；同一参数列表按固定顺序组成一批任务。</p></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <section className="panel"><h2>发起运行</h2><div className="form-grid"><label>实验草稿<select value={selected} onChange={event => { inputGeneration.current++; setSelected(event.target.value); prepared.current = null; preparingBatch.current = null; setRetry(false) }}><option value="">选择实验</option>{experiments.map(item => <option key={item.id} value={item.id}>{item.id} · 修订 {item.revision}</option>)}</select></label><label className="wide">参数列表 JSON<textarea value={parameters} onChange={event => { inputGeneration.current++; setParameters(event.target.value); prepared.current = null; preparingBatch.current = null; setRetry(false) }} placeholder={'[{"window":4},{"window":8}]'} /></label></div>
      <div className="inline-actions"><button type="button" disabled={busy} onClick={() => submit(false)}>运行</button><button type="button" disabled={busy} onClick={() => submit(true)}>运行参数列表</button>{retry && <button type="button" disabled={busy} onClick={retryPrepared}>重试提交</button>}{experimentCursor && <button type="button" onClick={loadMoreExperiments}>加载更多实验草稿</button>}</div><p className="muted">参数列表最多 100 组。先逐组保存为独立实验草稿，再提交一个批次；重试沿用已准备的草稿和幂等键。</p></section>
    <section className="panel"><div className="section-heading"><div><h2>运行记录</h2><p>运行列表和状态来自服务端；队列中的任务也可查看详情。任务时间按北京时间（Asia/Shanghai，UTC+8）显示。</p></div><button type="button" disabled={listBusy} onClick={refresh}>刷新</button></div><div className="form-grid"><label>状态筛选<select disabled={!listComplete} value={filter} onChange={event => setFilter(event.target.value as RunState | 'all')}><option value="all">全部</option>{Object.entries(stateLabel).map(([state, label]) => <option key={state} value={state}>{label}</option>)}</select></label><label>批次筛选<select disabled={!listComplete} value={groupFilter} onChange={event => setGroupFilter(event.target.value)}><option value="all">全部批次</option>{groupIds.map(group => <option key={group} value={group}>{group === 'single' ? '单次运行' : group}</option>)}</select></label></div>
      {!listComplete ? <><p role="status" className="callout">{listBusy ? `正在加载全部运行记录；已读取 ${runs.length} 条。筛选在读取完成后可用。` : `运行记录尚未读取完整；已读取 ${runs.length} 条。请重试。`}</p>{!listBusy && <button type="button" onClick={loadMoreRuns}>继续加载运行记录</button>}</> : visible.length === 0 ? <p className="empty">暂无匹配运行。</p> : [...groups].reverse().map(([group, items]) => <div key={group} className="run-group">{items[0].group_id && <h3 title={group}>批次 {group.slice(0, 8)}</h3>}<div className="record-list">{items.map(run => <RunSummary key={run.run_id} run={run} provenance={summaries[run.run_id]?.provenance || run.provenance} result={summaries[run.run_id]?.result} />)}</div></div>)}</section>
  </div>
}
