import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError, describeError } from '../api'
import { BasketRevisionSelect } from '../components/BasketRevisionSelect'
import { CapabilityReport } from '../components/CapabilityReport'
import { ParameterForm } from '../components/ParameterForm'
import { StrategyEditor } from '../components/StrategyEditor'
import type { BacktestInput, CapabilityReport as Report, TradingDefinitionRef } from '../frameworkTypes'
import type { DatasetVersion, ExperimentDraft, ExperimentPayload, JobRecord, RunRecord, StrategyRevision } from '../types'

// Explicit input scaffold. These numbers are visible starting points, not recommended account settings.
const inputSpec: BacktestInput = { start: '', end: '', initial_cash: 0, seed: 0, execution: { model: 'next_open', commission_rate: 0, minimum_commission: 0, slippage_rate: 0, max_stale_sessions: 0 }, risk: { max_total_weight: 1, max_symbol_weight: 1, group_caps: {}, stress_scenarios: [], pause_action: 'block_new', recovery_mode: 'never' }, metrics: { annual_sessions: 252, risk_free_rate: 0, cash_rate: 0, definition_version: '1' } }
const emptyPayload = (): ExperimentPayload => ({ framework_contract_version: 'multi-asset-v1', basket_revision_ref: { basket_id: '', revision: 0 }, trading_definition_ref: { kind: 'trading', id: '', version: '' }, strategy_revision_id: '', dataset_version_id: '', strategy_params: {}, backtest_spec: structuredClone(inputSpec), sample_purpose: '' })
const terminal = new Set(['succeeded', 'failed', 'cancelled', 'interrupted'])
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const unit = (value: unknown) => finite(value) && value >= 0 && value <= 1
const nonnegative = (value: unknown) => finite(value) && value >= 0
const appendUnique = <T extends { id: string }>(old: T[], next: T[]) => [...old, ...next.filter(item => !old.some(existing => existing.id === item.id))]
const validationStateLabel: Record<string, string> = { queued: '排队中', running: '运行中', cancel_requested: '等待取消', cancelled: '已取消', failed: '失败', interrupted: '中断' }
function specError(spec: BacktestInput): string {
  if (!Number.isInteger(spec.seed)) return '随机种子必须是整数。'
  if (!Number.isInteger(spec.execution.max_stale_sessions) || spec.execution.max_stale_sessions < 0) return '最大陈旧交易日必须是非负整数。'
  for (const [label, value] of [['佣金率', spec.execution.commission_rate], ['最低佣金', spec.execution.minimum_commission], ['滑点率', spec.execution.slippage_rate]] as const) if (!nonnegative(value)) return `${label}必须是非负数。`
  for (const [label, value] of [['总仓位上限', spec.risk.max_total_weight], ['单证券仓位上限', spec.risk.max_symbol_weight]] as const) if (!unit(value)) return `${label}必须在 0 到 1 之间。`
  if (!spec.risk.group_caps || Array.isArray(spec.risk.group_caps) || typeof spec.risk.group_caps !== 'object' || Object.values(spec.risk.group_caps).some(value => !unit(value))) return '分组仓位上限必须是 0 到 1 的数值映射。'
  if (!Array.isArray(spec.risk.stress_scenarios) || spec.risk.stress_scenarios.some(item => !item || !item.name || !nonnegative(item.execution_cost_rate) || !item.symbol_shocks || typeof item.symbol_shocks !== 'object' || Array.isArray(item.symbol_shocks) || Object.values(item.symbol_shocks).some(value => !finite(value)))) return '压力情景的名称、冲击值或执行费用无效。'
  if (spec.risk.max_stress_loss != null && (!nonnegative(spec.risk.max_stress_loss) || !spec.risk.stress_scenarios.length)) return '最大压力损失需要非负值和至少一个压力情景。'
  if (spec.risk.drawdown_pause != null && !unit(spec.risk.drawdown_pause)) return '回撤暂停阈值必须在 0 到 1 之间。'
  if (spec.risk.pause_action === 'reduce' ? !unit(spec.risk.pause_target_weight) : spec.risk.pause_target_weight != null) return '暂停目标权重只在降仓动作中使用，且须在 0 到 1 之间。'
  if (!Number.isInteger(spec.metrics.annual_sessions) || spec.metrics.annual_sessions <= 0) return '年化交易日数必须是正整数。'
  if (!finite(spec.metrics.risk_free_rate) || !finite(spec.metrics.cash_rate) || !spec.metrics.definition_version.trim()) return '指标利率须为有限数值，定义版本不能为空。'
  return ''
}

function JsonField({ label, value, onChange, onValidityChange }: { label: string; value: unknown; onChange: (next: unknown) => void; onValidityChange: (valid: boolean) => void }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2))
  const [error, setError] = useState('')
  useEffect(() => { setText(JSON.stringify(value, null, 2)); setError(''); onValidityChange(true) }, [value])
  return <label className="wide">{label}<textarea spellCheck={false} value={text} onChange={event => { setText(event.target.value); try { onChange(JSON.parse(event.target.value)); setError(''); onValidityChange(true) } catch { setError('JSON 格式未完成，不能保存'); onValidityChange(false) } }} />{error && <small className="error">{error}</small>}</label>
}

export function ExperimentsPage({ onDirtyChange, allowNextNavigation }: { onDirtyChange: (dirty: boolean) => void; allowNextNavigation: () => void }) {
  const { id } = useParams()
  const navigate = useNavigate()
  const [search] = useSearchParams()
  const editorIdentity = useRef(0)
  const editorDraftId = useRef<string | null>(null)
  const requestedDraftId = useRef<string | null>(null)
  const editGeneration = useRef(0)
  const [saving, setSaving] = useState(false)
  const [datasets, setDatasets] = useState<DatasetVersion[]>([])
  const [strategies, setStrategies] = useState<StrategyRevision[]>([])
  const [experiments, setExperiments] = useState<ExperimentDraft[]>([])
  const [datasetCursor, setDatasetCursor] = useState<string | null>(null)
  const [strategyCursor, setStrategyCursor] = useState<string | null>(null)
  const [experimentCursor, setExperimentCursor] = useState<string | null>(null)
  const [draft, setDraft] = useState<ExperimentDraft | null>(null)
  const [payload, setPayload] = useState<ExperimentPayload>(emptyPayload)
  const [tradingDefinitions, setTradingDefinitions] = useState<TradingDefinitionRef[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [checking, setChecking] = useState(false)
  const reportGeneration = useRef(0)
  const runAttempt = useRef<{ draftId: string; revision: number; key: string } | null>(null)
  const [loadingDraft, setLoadingDraft] = useState(Boolean(id))
  const [draftError, setDraftError] = useState('')
  const [dataset, setDataset] = useState<DatasetVersion | null>(null)
  const [strategy, setStrategy] = useState<StrategyRevision | null>(null)
  const [validation, setValidation] = useState<JobRecord | null>(null)
  const validationGeneration = useRef(0)
  const [dirtyDraft, setDirtyDraft] = useState(false)
  const [dirtySource, setDirtySource] = useState(false)
  const [message, setMessage] = useState('')
  const [conflictRevision, setConflictRevision] = useState<number | null>(null)
  const [conflictServer, setConflictServer] = useState<ExperimentDraft | null>(null)
  const [paramsValid, setParamsValid] = useState(true)
  const [structuredValid, setStructuredValid] = useState<Record<string, boolean>>({})
  const dirty = dirtyDraft || dirtySource
  useEffect(() => { onDirtyChange(dirty); const before = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = '' } }; window.addEventListener('beforeunload', before); return () => { window.removeEventListener('beforeunload', before); onDirtyChange(false) } }, [dirty])
  useEffect(() => { Promise.all([api.datasets(), api.strategies(), api.experiments()]).then(([data, source, saved]) => { setDatasets(data.items); setDatasetCursor(data.next_cursor); setStrategies(source.items); setStrategyCursor(source.next_cursor); setExperiments(old => appendUnique(saved.items, old)); setExperimentCursor(saved.next_cursor) }).catch(error => setMessage(describeError(error))) }, [])
  useEffect(() => { api.listDefinitions().then(value => setTradingDefinitions(value.items.filter(item => item.ref.kind === 'trading').map(item => item.ref as TradingDefinitionRef))).catch(error => setMessage(describeError(error))) }, [])
  const loadMoreDatasets = async () => { if (!datasetCursor) return; try { const page = await api.datasets(datasetCursor); setDatasets(old => appendUnique(old, page.items)); setDatasetCursor(page.next_cursor) } catch (error) { setMessage(describeError(error)) } }
  const loadMoreStrategies = async () => { if (!strategyCursor) return; try { const page = await api.strategies(strategyCursor); setStrategies(old => appendUnique(old, page.items)); setStrategyCursor(page.next_cursor) } catch (error) { setMessage(describeError(error)) } }
  const loadMoreExperiments = async () => { if (!experimentCursor) return; try { const page = await api.experiments(experimentCursor); setExperiments(old => appendUnique(old, page.items)); setExperimentCursor(page.next_cursor) } catch (error) { setMessage(describeError(error)) } }
  useEffect(() => { if (!payload.dataset_version_id) { setDataset(null); return }; let active = true; api.dataset(payload.dataset_version_id).then(value => { if (active) setDataset(value) }).catch(error => setMessage(describeError(error))); return () => { active = false } }, [payload.dataset_version_id])
  useEffect(() => { if (!payload.strategy_revision_id) { setStrategy(null); return }; let active = true; api.strategy(payload.strategy_revision_id).then(value => { if (active) setStrategy(value) }).catch(error => setMessage(describeError(error))); return () => { active = false } }, [payload.strategy_revision_id])
  useEffect(() => { if (!validation || terminal.has(validation.state)) return; const generation = validationGeneration.current; const timer = setInterval(() => { api.job(validation.id).then(job => { if (validationGeneration.current === generation) setValidation(current => current?.id === job.id ? job : current) }).catch(error => setMessage(describeError(error))) }, 1500); return () => clearInterval(timer) }, [validation?.id, validation?.state])
  const update = (next: ExperimentPayload) => { editGeneration.current += 1; validationGeneration.current += 1; reportGeneration.current += 1; runAttempt.current = null; setReport(null); setValidation(null); setPayload(next); setDirtyDraft(true); setMessage('') }
  const spec = payload.backtest_spec
  const setSpec = (next: BacktestInput) => update({ ...payload, backtest_spec: next })
  const setExecution = (next: BacktestInput['execution']) => setSpec({ ...spec, execution: next })
  const setRisk = (next: BacktestInput['risk']) => setSpec({ ...spec, risk: next })
  const setMetrics = (next: BacktestInput['metrics']) => setSpec({ ...spec, metrics: next })
  const selectDraft = async (id: string) => {
    const identity = ++editorIdentity.current
    requestedDraftId.current = id || null
    reportGeneration.current += 1; runAttempt.current = null; setReport(null)
    validationGeneration.current += 1; setMessage(''); setConflictRevision(null); setConflictServer(null); setValidation(null)
    setDraftError('')
    if (!id) { editorDraftId.current = null; setDraft(null); setPayload(emptyPayload()); setDirtyDraft(false); setDirtySource(false); setLoadingDraft(false); return }
    setLoadingDraft(true)
    try { const selected = await api.experiment(id); if (identity !== editorIdentity.current) return; editorDraftId.current = id; setExperiments(old => appendUnique(old, [selected])); setDraft(selected); setPayload(structuredClone(selected.payload)); setDirtyDraft(false); setDirtySource(false) } catch (error) { if (identity === editorIdentity.current) setDraftError(describeError(error)) } finally { if (identity === editorIdentity.current) setLoadingDraft(false) }
  }
  useEffect(() => { if (id && editorDraftId.current !== id) void selectDraft(id); else if (!id && (requestedDraftId.current || editorDraftId.current || draftError)) void selectDraft('') }, [id])
  useEffect(() => {
    const sourceId = !id ? search.get('source_run_id') : null
    if (!sourceId) return
    let active = true
    const generation = editGeneration.current
    api.run(sourceId).then((run: RunRecord) => {
      if (!active || generation !== editGeneration.current) return
      const provenance = run.provenance || {}
      const raw = provenance.backtest_spec
      if (!raw || typeof raw !== 'object') { setMessage('来源运行缺少回测条件，请手动填写新草稿。'); return }
      const { symbols: _symbols, ...spec } = raw as Record<string, unknown>
      update({ ...emptyPayload(), source_run_id: sourceId, dataset_version_id: String(provenance.dataset_version_id || ''), strategy_revision_id: String(provenance.strategy_revision_id || ''), strategy_params: (provenance.effective_strategy_params && typeof provenance.effective_strategy_params === 'object' ? provenance.effective_strategy_params : {}) as Record<string, unknown>, sample_purpose: String(provenance.sample_purpose || ''), backtest_spec: { ...structuredClone(inputSpec), ...spec } })
      setMessage('已载入回测条件。请选择标的组合和交易策略，再保存配置。')
    }).catch(error => { if (active && generation === editGeneration.current) setMessage(describeError(error)) })
    return () => { active = false }
  }, [id, search.get('source_run_id')])
  useEffect(() => () => { editorIdentity.current += 1 }, [])
  const save = async () => {
    if (saving || loadingDraft || draftError) return
    setMessage('')
    if (!paramsValid || Object.values(structuredValid).some(value => !value)) { setMessage('请先修正参数或风险配置中的 JSON。'); return }
    if (!payload.dataset_version_id || !payload.strategy_revision_id || !payload.basket_revision_ref.basket_id || !payload.basket_revision_ref.revision || !payload.trading_definition_ref.id || !spec.start || !spec.end || spec.start > spec.end || spec.initial_cash <= 0 || !payload.sample_purpose.trim()) { setMessage('请先选择数据、策略、标的组合和交易定义，填写有效日期、初始资金与样本用途。'); return }
    const invalidSpec = specError(spec)
    if (invalidSpec) { setMessage(invalidSpec); return }
    const identity = editorIdentity.current, generation = editGeneration.current
    setSaving(true)
    try {
      const saved = draft ? await api.saveExperiment(draft.id, draft.revision, payload) : await api.createExperiment(payload)
      runAttempt.current = null
      setExperiments(old => [...old.filter(item => item.id !== saved.id), saved])
      if (identity !== editorIdentity.current) return
      setDraft(saved); if (generation === editGeneration.current) { setPayload(structuredClone(saved.payload)); setDirtyDraft(false) }; setConflictRevision(null); setConflictServer(null); setMessage(`已保存实验修订 ${saved.revision}`)
      editorDraftId.current = saved.id
      requestedDraftId.current = saved.id
      if (!id) { allowNextNavigation(); navigate(`/experiments/${encodeURIComponent(saved.id)}`, { replace: true }) }
      try { const checked = await api.checkCapabilities({ operation: 'fixed_backtest', ...saved.payload }); if (identity === editorIdentity.current && generation === editGeneration.current) setReport(checked) }
      catch (error) { if (identity === editorIdentity.current && generation === editGeneration.current) { if (error instanceof ApiError && error.details.report) setReport(error.details.report as Report); else setMessage(`已保存实验修订 ${saved.revision}；能力检查失败：${describeError(error)}`) } }
    } catch (error) {
      if (identity !== editorIdentity.current) return
      if (error instanceof ApiError && error.details.report) setReport(error.details.report as Report)
      if (error instanceof ApiError && error.status === 409 && draft) {
        setConflictRevision(null); setConflictServer(null)
        try { const server = await api.experiment(draft.id); if (identity !== editorIdentity.current) return; setConflictRevision(server.revision); setConflictServer(server); setMessage(`保存冲突：服务端修订 ${server.revision}。本地修改已保留；请复制需要的内容后重新载入。`) } catch { setMessage('保存冲突：无法读取服务端修订，本地修改已保留。') }
      } else setMessage(describeError(error))
    } finally { setSaving(false) }
  }
  const checkCapabilities = async () => {
    const generation = ++reportGeneration.current
    setChecking(true); setReport(null)
    try { const checked = await api.checkCapabilities({ operation: 'fixed_backtest', ...payload }); if (generation === reportGeneration.current) setReport(checked) }
    catch (error) { if (generation === reportGeneration.current) { if (error instanceof ApiError && error.details.report) setReport(error.details.report as Report); setMessage(describeError(error)) } }
    finally { if (generation === reportGeneration.current) setChecking(false) }
  }
  const submitRun = async () => {
    if (!draft || dirty || report?.status !== 'ready' || saving) return
    if (!runAttempt.current || runAttempt.current.draftId !== draft.id || runAttempt.current.revision !== draft.revision) runAttempt.current = { draftId: draft.id, revision: draft.revision, key: crypto.randomUUID() }
    setSaving(true)
    try { const run = await api.submitRun(draft.id, draft.revision, runAttempt.current.key); runAttempt.current = null; setMessage(`已提交回测 ${run.run_id}`); navigate(`/runs/${encodeURIComponent(run.run_id)}`) }
    catch (error) { if (error instanceof ApiError && error.details.report) setReport(error.details.report as Report); setMessage(describeError(error)) }
    finally { setSaving(false) }
  }
  const selectStrategy = (id: string) => { if (dirtySource && !window.confirm('策略源码有未保存修改，确定切换吗？')) return; update({ ...payload, strategy_revision_id: id, strategy_params: {} }); setValidation(null) }
  const savedStrategy = (saved: StrategyRevision) => { setStrategies(old => [...old, saved]); update({ ...payload, strategy_revision_id: saved.id, strategy_params: {} }); setStrategy(saved); setDirtySource(false); setValidation(null) }
  const reloadServerDraft = async () => {
    if (!draft || !window.confirm('重新载入将丢弃本地未保存修改。确定继续吗？')) return
    try { const server = await api.experiment(draft.id); validationGeneration.current += 1; reportGeneration.current += 1; setReport(null); setValidation(null); setDraft(server); setPayload(structuredClone(server.payload)); setDirtyDraft(false); setConflictRevision(null); setConflictServer(null); setMessage(`已载入服务端修订 ${server.revision}`) } catch (error) { setMessage(describeError(error)) }
  }
  const validateStrategy = async () => { if (!paramsValid || dirtySource || !payload.strategy_revision_id) return; const generation = ++validationGeneration.current; setValidation(null); try { const job = await api.validateStrategy(payload.strategy_revision_id, payload.strategy_params); if (validationGeneration.current === generation) { setValidation(job); setMessage('策略验证已排队；有效性以独立工作进程结果为准。') } } catch (error) { if (validationGeneration.current === generation) setMessage(describeError(error)) } }
  const parameterValidityChanged = (valid: boolean) => { setParamsValid(valid); if (!valid) { editGeneration.current += 1; reportGeneration.current += 1; setReport(null); setDirtyDraft(true); validationGeneration.current += 1; setValidation(null) } }
  const sourceDirtyChanged = (dirty: boolean) => { setDirtySource(dirty); if (dirty) { reportGeneration.current += 1; setReport(null); validationGeneration.current += 1; setValidation(null) } }
  const number = (label: string, value: number | null | undefined, onChange: (value: number | null) => void, options?: { min?: number; step?: string; optional?: boolean }) => <label>{label}<input type="number" min={options?.min} step={options?.step || 'any'} value={value ?? ''} onChange={event => onChange(event.target.value === '' ? null : Number(event.target.value))} />{options?.optional && <small>留空表示不启用</small>}</label>
  if (loadingDraft) return <div className="page-stack"><Link to="/experiments">返回列表</Link><p>加载固定策略配置…</p></div>
  if (draftError) return <div className="page-stack"><Link to="/experiments">返回列表</Link><p role="alert" className="error callout">无法加载配置 {id}：{draftError}</p></div>
  return <div className="page-stack"><header className="page-heading"><Link to="/experiments">返回列表</Link><h1>{draft ? '编辑固定策略' : '新建固定策略'}</h1><p>选择标的组合、数据时段与策略参数，保存后即可检查并提交回测。</p></header>
    {message && <p role={message.includes('冲突') || message.includes('错误') ? 'alert' : 'status'} className={message.startsWith('已') ? 'success callout' : 'error callout'}>{message}</p>}
    {report && <CapabilityReport report={report} />}
    <section className="panel"><div className="section-heading"><div><h2>固定策略配置</h2><p>保存配置后由服务端检查运行能力。</p></div></div><div className="inline-actions"><label>已保存配置<select value={draft?.id || ''} onChange={event => navigate(event.target.value ? `/experiments/${encodeURIComponent(event.target.value)}` : '/experiments/new')}><option value="">新建配置</option>{experiments.map(item => <option value={item.id} key={item.id}>{String(strategies.find(strategy => strategy.id === item.payload.strategy_revision_id)?.manifest.name || '固定策略')} · {item.payload.backtest_spec.start}—{item.payload.backtest_spec.end}</option>)}</select></label>{experimentCursor && <button type="button" onClick={loadMoreExperiments}>加载更多配置</button>}<button type="button" onClick={() => { if (dirtySource) return; if (!dirty || window.confirm('当前修改尚未保存，仍要克隆吗？')) { editorIdentity.current += 1; editorDraftId.current = null; requestedDraftId.current = null; setDraft(null); setDirtyDraft(true); setConflictRevision(null); setConflictServer(null); setMessage('已克隆到未保存的新配置，请保存以创建独立配置。'); allowNextNavigation(); navigate('/experiments/new') } }} disabled={!draft || dirtySource}>复制为新配置</button><button type="button" onClick={save} disabled={saving}>保存配置</button><button type="button" onClick={() => void checkCapabilities()} disabled={checking || dirtySource}>检查运行能力</button><button type="button" onClick={() => void submitRun()} disabled={!draft || dirty || saving || report?.status !== 'ready'}>提交回测</button></div>{dirtySource && <p className="callout">请先保存策略源码，或切换配置并确认放弃源码修改，再复制。</p>}{dirtyDraft && <p className="badge warn">配置未保存</p>}{conflictRevision !== null && <p>配置已在别处更新；本地输入保持原样。<button type="button" onClick={reloadServerDraft} disabled={saving}>重新载入</button></p>}</section>
    <section className="panel"><h2>标的组合与交易策略</h2><BasketRevisionSelect value={payload.basket_revision_ref.basket_id ? payload.basket_revision_ref : null} onChange={value => update({ ...payload, basket_revision_ref: value || { basket_id: '', revision: 0 } })} /><label>交易策略<select value={payload.trading_definition_ref.id ? `${payload.trading_definition_ref.id}:${payload.trading_definition_ref.version}` : ''} onChange={event => update({ ...payload, trading_definition_ref: tradingDefinitions.find(item => `${item.id}:${item.version}` === event.target.value) || { kind: 'trading', id: '', version: '' } })}><option value="">请选择交易策略</option>{tradingDefinitions.map(item => <option value={`${item.id}:${item.version}`} key={`${item.id}:${item.version}`}>{item.id}</option>)}</select></label></section>
    <div className="two-column"><section className="panel"><h2>数据版本</h2><label>选择数据<select value={payload.dataset_version_id} onChange={event => update({ ...payload, dataset_version_id: event.target.value })}><option value="">请选择已验证版本</option>{datasets.map(item => <option value={item.id} key={item.id}>{item.manifest.source} · {item.id.slice(0, 8)}</option>)}</select></label>{datasetCursor && <button type="button" onClick={loadMoreDatasets}>加载更多数据版本</button>}{dataset ? <div className="record compact"><p>{dataset.manifest.is_demo ? '演示数据' : '真实来源声明'} · {dataset.manifest.price_mode === 'raw_cash' ? '原始价格现金账户' : '复权研究价'}</p><p>价格 {dataset.manifest.data_start} — {dataset.manifest.data_end}</p><p>日历 {dataset.manifest.calendar_start} — {dataset.manifest.calendar_end} · 事件 {dataset.manifest.event_coverage}</p>{dataset.manifest.limitations?.length > 0 && <p>限制：{dataset.manifest.limitations.join('；')}</p>}</div> : <p className="empty">请选择数据版本。</p>}</section>
      <section className="panel"><h2>策略</h2><label>选择策略<select value={payload.strategy_revision_id} onChange={event => selectStrategy(event.target.value)}><option value="">新建策略</option>{strategies.map(item => <option value={item.id} key={item.id}>{String(item.manifest.name || item.manifest.entrypoint)}</option>)}</select></label>{strategyCursor && <button type="button" onClick={loadMoreStrategies}>加载更多策略</button>}<button disabled={!payload.strategy_revision_id || dirtySource || !paramsValid} onClick={validateStrategy}>验证当前策略与参数</button>{validation && <p>{validation.state === 'succeeded' && validation.result?.valid === true ? `策略有效 · 预热 ${validation.result.warmup_bars} 根` : validation.state === 'succeeded' ? '策略未通过验证' : `验证任务 ${validationStateLabel[validation.state] || validation.state}`}{validation.error ? ` · ${validation.error}` : ''}</p>}<p className="muted">{dataset && spec.start ? `数据从 ${dataset.manifest.data_start} 开始，回测从 ${spec.start} 开始。` : ''}起始日前所需日线由运行时核验。</p></section></div>
    {conflictServer && <section className="panel"><h2>服务端实验内容（只读）· 修订 {conflictServer.revision}</h2><textarea aria-label="服务端实验内容" readOnly rows={16} value={JSON.stringify(conflictServer.payload, null, 2)} /></section>}
    <StrategyEditor key={`${editorIdentity.current}:${payload.strategy_revision_id}`} revision={strategy} onSaved={savedStrategy} onDirtyChange={sourceDirtyChanged} />
    <section className="panel"><h2>策略参数</h2>{strategy ? <ParameterForm key={`${editorIdentity.current}:${strategy.id}`} schema={strategy.manifest.parameter_schema} value={payload.strategy_params} onChange={value => update({ ...payload, strategy_params: value })} onValidityChange={parameterValidityChanged} /> : <p className="empty">选择或保存策略后编辑参数。</p>}</section>
    <section className="panel"><h2>样本与回测范围</h2><div className="form-grid"><label>样本用途<input value={payload.sample_purpose} onChange={event => update({ ...payload, sample_purpose: event.target.value })} placeholder="例如：探索 / 验证" /></label><label>绩效起始日<input type="date" value={spec.start} onChange={event => setSpec({ ...spec, start: event.target.value })} /></label><label>结束日<input type="date" value={spec.end} onChange={event => setSpec({ ...spec, end: event.target.value })} /></label>{number('初始资金', spec.initial_cash, value => setSpec({ ...spec, initial_cash: value || 0 }), { min: 0 })}{number('随机种子', spec.seed, value => setSpec({ ...spec, seed: value || 0 }), { step: '1' })}</div></section>
    <section className="panel"><h2>执行与费用</h2><p>执行模型固定为下一个交易日开盘撮合（next_open）。</p><div className="form-grid">{number('佣金率', spec.execution.commission_rate, value => setExecution({ ...spec.execution, commission_rate: value || 0 }), { min: 0 })}{number('最低佣金', spec.execution.minimum_commission, value => setExecution({ ...spec.execution, minimum_commission: value || 0 }), { min: 0 })}{number('滑点率', spec.execution.slippage_rate, value => setExecution({ ...spec.execution, slippage_rate: value || 0 }), { min: 0 })}{number('最大陈旧交易日', spec.execution.max_stale_sessions, value => setExecution({ ...spec.execution, max_stale_sessions: value || 0 }), { min: 0, step: '1' })}</div></section>
    <section className="panel"><h2>风险假设</h2><div className="form-grid">{number('总仓位上限', spec.risk.max_total_weight, value => setRisk({ ...spec.risk, max_total_weight: value || 0 }), { min: 0 })}{number('单证券仓位上限', spec.risk.max_symbol_weight, value => setRisk({ ...spec.risk, max_symbol_weight: value || 0 }), { min: 0 })}{number('最大压力损失', spec.risk.max_stress_loss, value => setRisk({ ...spec.risk, max_stress_loss: value }), { min: 0, optional: true })}{number('回撤暂停阈值', spec.risk.drawdown_pause, value => setRisk({ ...spec.risk, drawdown_pause: value }), { min: 0, optional: true })}<label>暂停动作<select value={spec.risk.pause_action} onChange={event => setRisk({ ...spec.risk, pause_action: event.target.value as BacktestInput['risk']['pause_action'], pause_target_weight: event.target.value === 'reduce' ? spec.risk.pause_target_weight ?? 0 : null })}><option value="block_new">阻止新买入</option><option value="reduce">降仓</option><option value="liquidate">清仓</option></select></label>{spec.risk.pause_action === 'reduce' && number('暂停目标权重', spec.risk.pause_target_weight, value => setRisk({ ...spec.risk, pause_target_weight: value }), { min: 0 })}<label>恢复方式<select value={spec.risk.recovery_mode} onChange={event => setRisk({ ...spec.risk, recovery_mode: event.target.value as BacktestInput['risk']['recovery_mode'] })}><option value="never">不自动恢复</option><option value="recorded_approval">记录批准后恢复</option></select></label><JsonField label="分组仓位上限 · JSON" value={spec.risk.group_caps} onChange={value => setRisk({ ...spec.risk, group_caps: value as Record<string, number> })} onValidityChange={valid => { if (!valid) { editGeneration.current += 1; setDirtyDraft(true) }; setStructuredValid(old => old.caps === valid ? old : { ...old, caps: valid }) }} /><JsonField label="压力情景 · JSON" value={spec.risk.stress_scenarios} onChange={value => setRisk({ ...spec.risk, stress_scenarios: value as BacktestInput['risk']['stress_scenarios'] })} onValidityChange={valid => { if (!valid) { editGeneration.current += 1; setDirtyDraft(true) }; setStructuredValid(old => old.scenarios === valid ? old : { ...old, scenarios: valid }) }} /></div></section>
    <section className="panel"><h2>指标口径</h2><div className="form-grid">{number('年化交易日数', spec.metrics.annual_sessions, value => setMetrics({ ...spec.metrics, annual_sessions: value || 0 }), { min: 1, step: '1' })}{number('无风险利率', spec.metrics.risk_free_rate, value => setMetrics({ ...spec.metrics, risk_free_rate: value || 0 }) )}{number('现金利率', spec.metrics.cash_rate, value => setMetrics({ ...spec.metrics, cash_rate: value || 0 }) )}</div></section>
  </div>
}
