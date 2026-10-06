import { useEffect, useRef, useState } from 'react'
import Ajv2020 from 'ajv/dist/2020.js'
import { api, describeError } from '../api'
import { ParameterForm } from './ParameterForm'
import type { BacktestSpec, DatasetVersion, ExperimentDraft, ExperimentPayload, Page, ParameterSchema, StrategyRevision } from '../types'

type ParameterGroup = { id: string; params: Record<string, unknown>; valid: boolean }
type Prepared = { payloads: ExperimentPayload[]; items: { experiment_id: string; expected_revision: number }[]; key: string }
const emptySpec: BacktestSpec = { start: '', end: '', symbols: [], initial_cash: 0, seed: 0, execution: { model: 'next_open', commission_rate: 0, minimum_commission: 0, slippage_rate: 0, max_stale_sessions: 0 }, risk: { max_total_weight: 1, max_symbol_weight: 1, group_caps: {}, stress_scenarios: [], pause_action: 'block_new', recovery_mode: 'never' }, metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: '1' } }
const group = (params: Record<string, unknown>): ParameterGroup => ({ id: crypto.randomUUID(), params: structuredClone(params), valid: true })
const strategyName = (item: StrategyRevision) => String(item.manifest.name || item.manifest.entrypoint)

async function allPages<T>(load: (cursor?: string) => Promise<Page<T>>) {
  const rows: T[] = []; let cursor: string | undefined
  for (;;) {
    const page = await load(cursor)
    rows.push(...page.items)
    if (!page.next_cursor) return rows
    if (page.next_cursor === cursor) throw new Error('列表游标未前进。')
    cursor = page.next_cursor
  }
}

export function FixedBacktestForm({ onSubmitted }: { onSubmitted: () => void }) {
  const [strategies, setStrategies] = useState<StrategyRevision[]>([]), [datasets, setDatasets] = useState<DatasetVersion[]>([]), [drafts, setDrafts] = useState<ExperimentDraft[]>([])
  const [strategyId, setStrategyId] = useState(''), [datasetId, setDatasetId] = useState('')
  const [spec, setSpec] = useState<BacktestSpec>(() => structuredClone(emptySpec)), [purpose, setPurpose] = useState('探索')
  const [groups, setGroups] = useState<ParameterGroup[]>([])
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [retry, setRetry] = useState(false)
  const [error, setError] = useState(''), [notice, setNotice] = useState('')
  const prepared = useRef<Prepared | null>(null), locked = useRef(false), mounted = useRef(false)
  const strategy = strategies.find(item => item.id === strategyId), dataset = datasets.find(item => item.id === datasetId)
  const saved = drafts.find(item => item.payload.strategy_revision_id === strategyId)

  useEffect(() => {
    mounted.current = true
    Promise.all([allPages(api.strategies), allPages(api.datasets), allPages(api.experiments)]).then(([source, data, configs]) => {
      if (!mounted.current) return
      setStrategies(source.reverse()); setDatasets(data.reverse()); setDrafts(configs.reverse()); setLoading(false)
    }).catch(value => { if (mounted.current) { setError(describeError(value)); setLoading(false) } })
    return () => { mounted.current = false }
  }, [])

  const edited = () => { prepared.current = null; setRetry(false); setError(''); setNotice('') }
  const chooseStrategy = (id: string) => {
    edited(); setStrategyId(id)
    const source = strategies.find(item => item.id === id), config = drafts.find(item => item.payload.strategy_revision_id === id)
    const data = datasets.find(item => item.id === config?.payload.dataset_version_id) || datasets[0]
    setDatasetId(config?.payload.dataset_version_id || data?.id || '')
    setSpec(config ? structuredClone(config.payload.backtest_spec) : { ...structuredClone(emptySpec), start: data?.manifest.data_start || '', end: data?.manifest.data_end || '' })
    setPurpose(config?.payload.sample_purpose || '探索')
    const defaults = Object.fromEntries(Object.entries(source?.manifest.parameter_schema.properties || {}).filter(([, field]) => field.default !== undefined).map(([key, field]) => [key, field.default]))
    setGroups(source ? [group({ ...defaults, ...config?.payload.strategy_params })] : [])
  }
  const updateSpec = (next: BacktestSpec) => { edited(); setSpec(next) }
  const submit = async (isRetry = false) => {
    if (locked.current) return
    locked.current = true; setBusy(true); setError(''); setNotice('')
    try {
      if (!isRetry || !prepared.current) {
        if (!strategy || !dataset) throw new Error('请选择策略和日线数据。')
        if (!/^\d{4}-\d{2}-\d{2}$/.test(spec.start) || !/^\d{4}-\d{2}-\d{2}$/.test(spec.end) || spec.start > spec.end) throw new Error('请填写有效的回测起止日期。')
        if (spec.start < dataset.manifest.data_start || spec.end > dataset.manifest.data_end) throw new Error('回测日期超出数据覆盖范围。')
        if (!spec.symbols.length || !Number.isFinite(spec.initial_cash) || spec.initial_cash <= 0 || !purpose.trim()) throw new Error('请填写证券代码、正数初始资金和样本用途。')
        if (!Number.isInteger(spec.seed) || !Number.isInteger(spec.execution.max_stale_sessions) || spec.execution.max_stale_sessions < 0) throw new Error('随机种子须为整数，最大陈旧交易日须为非负整数。')
        if ([spec.execution.commission_rate, spec.execution.minimum_commission, spec.execution.slippage_rate].some(value => !Number.isFinite(value) || value < 0)) throw new Error('佣金和滑点须为非负数。')
        if ([spec.risk.max_total_weight, spec.risk.max_symbol_weight].some(value => !Number.isFinite(value) || value < 0 || value > 1)) throw new Error('仓位上限须在0到1之间。')
        if (!groups.length || groups.length > 100) throw new Error('参数最多100组。')
        const schema = structuredClone(strategy.manifest.parameter_schema)
        delete (schema as ParameterSchema & { $schema?: string }).$schema
        const close = (node: ParameterSchema) => {
          if (node.type === 'object') { (node as ParameterSchema & { additionalProperties: boolean }).additionalProperties = false; Object.values(node.properties || {}).forEach(close) }
          if (node.type === 'array' && node.items) close(node.items)
        }
        close(schema)
        const validate = new Ajv2020({ allErrors: true, useDefaults: true, strict: false }).compile(schema)
        const payloads = groups.map((item, index) => {
          const params = structuredClone(item.params)
          if (!item.valid || !validate(params)) throw new Error(`第${index + 1}组参数无效：${validate.errors?.[0]?.message || '请检查字段'}`)
          return { strategy_revision_id: strategyId, dataset_version_id: datasetId, strategy_params: params, backtest_spec: structuredClone(spec), sample_purpose: purpose.trim() }
        })
        prepared.current = { payloads, items: [], key: crypto.randomUUID() }
      }
      const request = prepared.current!
      for (const payload of request.payloads.slice(request.items.length)) {
        const variant = await api.createExperiment(payload)
        request.items.push({ experiment_id: variant.id, expected_revision: variant.revision })
        if (!mounted.current) return
      }
      if (request.items.length > 1) await api.submitBatch(request.items, request.key)
      else await api.submitRun(request.items[0].experiment_id, request.items[0].expected_revision, request.key)
      if (!mounted.current) return
      setRetry(false); setNotice(`已提交${request.items.length}组回测。`); prepared.current = null; onSubmitted()
    } catch (value) { if (mounted.current) { setError(describeError(value)); setRetry(!!prepared.current) } }
    finally { locked.current = false; if (mounted.current) setBusy(false) }
  }

  return <section className="panel"><h2>发起固定策略回测</h2>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="success">{notice}</p>}
    {loading && <p role="status">加载策略与数据…</p>}
    <form noValidate onSubmit={event => { event.preventDefault(); void submit() }}><fieldset disabled={busy || loading}>
      <div className="form-grid">
        <label className="wide">策略<select value={strategyId} onChange={event => chooseStrategy(event.target.value)}><option value="">请选择策略</option>{strategies.map(item => {
          const revisions = strategies.filter(value => strategyName(value) === strategyName(item))
          return <option key={item.id} value={item.id}>{strategyName(item)}{revisions.length > 1 ? ` · 版本${revisions.length - revisions.indexOf(item)}` : ''}</option>
        })}</select></label>
        <label>回测开始<input type="date" min={dataset?.manifest.data_start} max={dataset?.manifest.data_end} value={spec.start} onChange={event => updateSpec({ ...spec, start: event.target.value })} /></label>
        <label>回测结束<input type="date" min={dataset?.manifest.data_start} max={dataset?.manifest.data_end} value={spec.end} onChange={event => updateSpec({ ...spec, end: event.target.value })} /></label>
      </div>
      {strategy && <>
        {saved && <p className="muted">已带入此策略最近保存的配置，可直接修改。</p>}
        <details open={!saved}><summary>回测条件 · {spec.symbols.length}只证券 · 初始资金 {spec.initial_cash}</summary><div className="form-grid">
          <label>日线数据<select value={datasetId} onChange={event => { edited(); setDatasetId(event.target.value) }}><option value="">请选择数据</option>{datasets.map(item => <option key={item.id} value={item.id}>{item.manifest.source} · {item.manifest.data_start}–{item.manifest.data_end} · {item.id.slice(0, 8)}</option>)}</select></label>
          <label>证券代码<input value={spec.symbols.join(',')} placeholder="逗号分隔，如 512480,515880" onChange={event => updateSpec({ ...spec, symbols: [...new Set(event.target.value.split(/[,，\s]+/).filter(Boolean))] })} /></label>
          <label>初始资金<input type="number" min={0} value={spec.initial_cash || ''} onChange={event => updateSpec({ ...spec, initial_cash: Number(event.target.value) })} /></label>
          <label>样本用途<input value={purpose} onChange={event => { edited(); setPurpose(event.target.value) }} /></label>
          <label>随机种子<input type="number" step={1} value={spec.seed} onChange={event => updateSpec({ ...spec, seed: Number(event.target.value) })} /></label>
          {([['commission_rate', '佣金率'], ['minimum_commission', '最低佣金'], ['slippage_rate', '滑点率'], ['max_stale_sessions', '最大陈旧交易日']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" min={0} step={key === 'max_stale_sessions' ? 1 : 'any'} value={spec.execution[key]} onChange={event => updateSpec({ ...spec, execution: { ...spec.execution, [key]: Number(event.target.value) } })} /></label>)}
          {([['max_total_weight', '总仓位上限'], ['max_symbol_weight', '单证券仓位上限']] as const).map(([key, label]) => <label key={key}>{label}<input type="number" min={0} max={1} step="any" value={spec.risk[key]} onChange={event => updateSpec({ ...spec, risk: { ...spec.risk, [key]: Number(event.target.value) } })} /></label>)}
        </div>{saved && <p className="muted">其余风控和指标设置沿用已保存配置。</p>}</details>
        <h3>策略参数</h3>
        {groups.map((item, index) => <fieldset className="record" key={item.id} aria-label={`第${index + 1}组参数`}><div className="record-head"><strong>第{index + 1}组参数</strong>{groups.length > 1 && <button type="button" onClick={() => { edited(); setGroups(old => old.filter(value => value.id !== item.id)) }}>删除这组</button>}</div>
          <ParameterForm key={item.id} schema={strategy.manifest.parameter_schema} value={item.params} showJson={false} idPrefix={`fixed-${item.id}`} onChange={params => { edited(); setGroups(old => old.map(value => value.id === item.id ? { ...value, params } : value)) }} onValidityChange={valid => setGroups(old => old.some(value => value.id === item.id && value.valid !== valid) ? old.map(value => value.id === item.id ? { ...value, valid } : value) : old)} />
        </fieldset>)}
        <div className="inline-actions">{!!Object.keys(strategy.manifest.parameter_schema.properties || {}).length && <button type="button" disabled={groups.length >= 100} onClick={() => { edited(); setGroups(old => [...old, group(old[old.length - 1].params)]) }}>添加一组参数</button>}<button type="submit">{busy ? '提交中…' : groups.length > 1 ? `运行${groups.length}组回测` : '开始回测'}</button>{retry && <button type="button" onClick={() => void submit(true)}>重试提交</button>}</div>
      </>}
      {!loading && !strategies.length && <p>暂无策略，请先在固定策略页面创建策略。</p>}
    </fieldset></form>
  </section>
}
