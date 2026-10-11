import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, api, describeError } from '../api'
import { CapabilityReport as CapabilityReportView } from '../components/CapabilityReport'
import { CurrentModelSnapshot } from '../components/CurrentModelSnapshot'
import { JsonDetails, Metrics, metricText } from '../components/LearningResults'
import { taskTime } from '../components/timePresentation'
import type { InvestingArtifact, InvestingModelSummary, TrainingHistoryItem, TrainingHistoryPage, TrainingHistoryQuery } from '../investingTypes'
import type { RunEvent } from '../types'
import type { CapabilityReport } from '../frameworkTypes'

const stateLabels: Record<string, string> = { queued: '排队中', running: '运行中', cancel_requested: '取消中', cancelled: '已取消', interrupted: '已中断', failed: '失败', succeeded: '成功' }
type PanelState = { page: number; size: number; anchor?: number; data: TrainingHistoryPage | null; loading: boolean; error: string; lookup?: number; request: number }
const initialPanel = (): PanelState => ({ page: 1, size: 10, data: null, loading: true, error: '', request: 0 })
const primaryMetrics = [['total_return', '总收益率'], ['max_drawdown', '最大回撤（日末）'], ['sharpe', '夏普比率（日末）']] as const

function snapshotStatus(item: TrainingHistoryItem) {
  if (item.selection_valid === false) return '暂无有效最优快照'
  if (item.selection_valid !== true) return '最优快照状态未记录'
  if (item.best_round_number == null) return '最优快照来源未记录'
  if (item.best_round_number === item.round_number) return item.round_number === 1 ? '首次建立最优快照' : '本轮更新最优快照'
  return `未切换 · 沿用第${item.best_round_number}轮`
}

function trainingSamplingIntervals(manifest: Record<string, unknown>) {
  const value = manifest.training_sampling_intervals
  if (!Array.isArray(value)) return []
  return value.filter((interval): interval is { start: string; end: string } =>
    typeof interval === 'object' && interval !== null &&
    typeof (interval as Record<string, unknown>).start === 'string' &&
    typeof (interval as Record<string, unknown>).end === 'string')
}

function TrainingSamplingLabel({ item }: { item: TrainingHistoryItem }) {
  const sampling = item.training_sampling
  return sampling
    ? <p className="muted">实际采样覆盖：{sampling.start}–{sampling.end} · {sampling.episode_count} 个片段</p>
    : <p className="muted">实际采样区间：未记录</p>
}

function Pagination({ label, value, onPage, onSize, onJump, compact = false }: { label: string; value: PanelState; onPage: (page: number) => void; onSize?: (size: number) => void; onJump?: (page: number) => void; compact?: boolean }) {
  const [pageInput, setPageInput] = useState('')
  const [pageError, setPageError] = useState('')
  const data = value.data
  const page = data?.page ?? value.page
  const size = data?.page_size ?? value.size
  const totalPages = data?.total_pages || 1
  if (data?.total === 0) return null
  if (compact) return <div className="history-pagination compact" aria-label={`${label}翻页下方`}>
    <button disabled={value.loading || page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
    <span>第{page}页 / 共{totalPages}页</span>
    <button disabled={value.loading || page >= totalPages} onClick={() => onPage(page + 1)}>下一页</button>
  </div>
  return <>
    <div className="history-pagination" aria-label={`${label}翻页上方`}>
      <button disabled={value.loading || page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
      <span>第{page}页 / 共{totalPages}页 · 显示 {(page - 1) * size + 1}–{Math.min(page * size, data?.total || 0)} 条，共 {data?.total || 0} 条</span>
      <button disabled={value.loading || page >= totalPages} onClick={() => onPage(page + 1)}>下一页</button>
      {onSize && <label>每页条数<select aria-label={`${label}每页条数`} disabled={value.loading} value={size} onChange={event => onSize(Number(event.target.value))}>{[10, 20, 50].map(size => <option key={size}>{size}</option>)}</select></label>}
      {onJump && <form onSubmit={event => { event.preventDefault(); const page = Number(pageInput); if (Number.isInteger(page) && page >= 1 && page <= totalPages) { onJump(page); setPageError('') } else setPageError(`请输入 1 到 ${totalPages} 之间的页码。`); setPageInput('') }}>
        <label>跳转页码<input aria-label={`${label}跳转页码`} inputMode="numeric" value={pageInput} onChange={event => setPageInput(event.target.value)} /></label>
        <button disabled={value.loading}>跳转</button>
        {pageError && <span role="alert">{pageError}</span>}
      </form>}
    </div>
  </>
}

export function ModelDetailPage() {
  const { id = '' } = useParams()
  return <ModelDetailContent key={id} id={id} />
}

function ModelDetailContent({ id }: { id: string }) {
  const [model, setModel] = useState<InvestingModelSummary | null>(null)
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0), [budget, setBudget] = useState(4096), [device, setDevice] = useState<'cpu' | 'cuda'>('cpu')
  const [cudaAvailable, setCudaAvailable] = useState(false)
  const [trainReport, setTrainReport] = useState<CapabilityReport | null>(null), [trainCheckedKey, setTrainCheckedKey] = useState('')
  const [history, setHistory] = useState<PanelState>(initialPanel)
  const [expanded, setExpanded] = useState(''), [artifacts, setArtifacts] = useState<Record<string, InvestingArtifact>>({}), [artifactErrors, setArtifactErrors] = useState<Record<string, string>>({})
  const [selectedLog, setSelectedLog] = useState<TrainingHistoryItem | null>(null), [logs, setLogs] = useState<RunEvent[]>([]), [logError, setLogError] = useState('')
  const [roundInput, setRoundInput] = useState('')
  const [artifactLoading, setArtifactLoading] = useState(''), [artifactRetry, setArtifactRetry] = useState(0)
  const initializedModel = useRef(''), focusTarget = useRef(''), previousActive = useRef<string | null>(null)
  const activeId = model?.active_job_id
  const trainKey = JSON.stringify({ id, budget, device, binding: model?.framework_binding?.binding_hash })
  const latestTrainKey = useRef(trainKey)
  latestTrainKey.current = trainKey
  const validTrainReport = trainCheckedKey === trainKey ? trainReport : null
  const newCount = history.data ? Math.max(0, (model?.session_count || 0) - history.data.total) : 0
  const logActive = !!selectedLog && selectedLog.id === activeId

  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      let delay = 10000
      try {
        const value = await api.investingModelSummary(id, controller.signal)
        if (controller.signal.aborted) return
        setModel(value); setError(''); setCudaAvailable(value.capabilities?.cuda_available || false)
        if (initializedModel.current !== id) { setBudget(value.spec.total_timesteps); setDevice(value.spec.device); initializedModel.current = id }
        if (value.active_job_id) delay = 3000
        if (previousActive.current && previousActive.current !== value.active_job_id) {
          setHistory(old => ({ ...old, request: old.request + 1 }))
        }
        previousActive.current = value.active_job_id
      } catch (value) { if (!controller.signal.aborted) setError(describeError(value)) }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, delay) }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [id, refresh])

  const fetchHistory = (value: PanelState) => {
    const controller = new AbortController()
    setHistory(old => ({ ...old, loading: true, error: '' }))
    const query: TrainingHistoryQuery = { kind: 'sessions', page: value.page, page_size: value.size, anchor: value.anchor }
    if (value.lookup !== undefined) {
      query.round_number = value.lookup
      delete query.anchor
    }
    api.investingModelHistory(id, query, controller.signal).then(data => {
      if (controller.signal.aborted) return
      setHistory(old => ({ ...old, page: data.page, anchor: old.lookup === undefined ? (old.anchor ?? data.anchor) : data.anchor, data, loading: false, lookup: undefined }))
      if (value.lookup !== undefined) {
        const target = data.items.find(item => item.round_number === value.lookup)
        if (target) {
          setExpanded(target.id)
          focusTarget.current = `history-${target.id}`
        } else setHistory(old => ({ ...old, error: `未找到第${value.lookup}轮。` }))
      }
    }).catch(reason => { if (!controller.signal.aborted) setHistory(old => ({ ...old, loading: false, error: describeError(reason) })) })
    return () => controller.abort()
  }
  useEffect(() => fetchHistory(history), [id, refresh, history.request])
  useEffect(() => {
    if (!focusTarget.current) return
    const target = document.getElementById(focusTarget.current)
    if (target) { target.focus({ preventScroll: true }); target.scrollIntoView?.({ block: 'center' }); focusTarget.current = '' }
  }, [history.data, selectedLog])
  useEffect(() => {
    if (!expanded || artifacts[expanded]) return
    const controller = new AbortController()
    setArtifactLoading(expanded); setArtifactErrors(old => ({ ...old, [expanded]: '' }))
    api.investingArtifact('policies', expanded, controller.signal).then(value => {
      if (!controller.signal.aborted) setArtifacts(old => ({ ...old, [expanded]: value }))
    }).catch(reason => {
      if (!controller.signal.aborted) setArtifactErrors(old => ({ ...old, [expanded]: describeError(reason) }))
    }).finally(() => { if (!controller.signal.aborted) setArtifactLoading('') })
    return () => controller.abort()
  }, [expanded, artifactRetry])

  useEffect(() => {
    if (!selectedLog) return
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>; let cursor: string | undefined
    setLogs([]); setLogError('')
    const load = async () => {
      try {
        const page = await api.jobLogs(selectedLog.id, cursor, controller.signal)
        if (controller.signal.aborted) return
        setLogs(old => [...old, ...page.items].filter((event, index, array) => array.findIndex(other => other.seq === event.seq) === index).slice(-500)); setLogError('')
        if (page.next_cursor !== null) cursor = page.next_cursor
        if (page.items.length >= 100 || logActive) timer = setTimeout(load, 2000)
      } catch (value) { if (!controller.signal.aborted) setLogError(describeError(value)) }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [selectedLog, logActive])

  const checkTraining = async () => {
    setError(''); setTrainReport(null); setTrainCheckedKey('')
    if (!model?.framework_binding) { setError('模型缺少可验证的框架关联。'); return }
    if (!Number.isInteger(budget) || budget < 2 || budget > 10000000) { setError('追加决策步数须为 2 到 10000000 的整数。'); return }
    const key = latestTrainKey.current
    setBusy(true)
    try {
      const report = await api.checkCapabilities({ operation: 'resume', model_id: id, additional_timesteps: budget, device })
      if (latestTrainKey.current === key) { setTrainReport(report); setTrainCheckedKey(key) }
    } catch (value) { setError(describeError(value)) }
    finally { setBusy(false) }
  }
  const submitTraining = async () => {
    if (!model || activeId || busy) return
    if (validTrainReport?.status !== 'ready') return
    setError(''); setBusy(true)
    try {
      if (!Number.isInteger(budget) || budget < 2 || budget > 10000000) throw new Error('追加决策步数必须是 2–10000000 的整数。')
      const job = await api.trainInvestingModel(id, { additional_timesteps: budget, device })
      setModel(old => old && ({ ...old, active_job_id: job.id }))
      setNotice('训练已提交。'); setRefresh(value => value + 1)
    } catch (value) {
      const report = value instanceof ApiError ? value.details.report : null
      if (report && typeof report === 'object' && 'status' in report) { setTrainReport(report as CapabilityReport); setTrainCheckedKey(latestTrainKey.current) }
      setError(describeError(value))
    }
    finally { setBusy(false) }
  }
  const cancelTraining = async () => {
    if (!activeId || busy) return
    setBusy(true); setError('')
    try { await api.cancelJob(activeId); setNotice('已请求取消训练；本次未发布的进度将丢弃。'); setRefresh(value => value + 1) }
    catch (value) { setError(describeError(value)) }
    finally { setBusy(false) }
  }
  const updateHistory = (next: Partial<PanelState>) => setHistory(old => ({ ...old, ...next, request: old.request + 1 }))
  const setPageSize = (size: number) => {
    const firstIndex = ((history.data?.page ?? history.page) - 1) * (history.data?.page_size ?? history.size)
    updateHistory({ size, page: Math.floor(firstIndex / size) + 1, anchor: history.data?.anchor, lookup: undefined })
  }
  const lookupRound = (raw: string) => {
    const number = Number(raw)
    if (!Number.isInteger(number) || number < 1) { setHistory(old => ({ ...old, error: '请输入大于 0 的整数。' })); return }
    const count = model?.version_count || 0
    if (number > count) { setHistory(old => ({ ...old, error: `当前共有 ${count} 轮。` })); return }
    updateHistory({ anchor: undefined, lookup: number, error: '' })
  }
  const showLatest = () => updateHistory({ page: 1, anchor: undefined, lookup: undefined })

  const renderPagination = (compact = false) => <Pagination label="训练历史" value={history} compact={compact}
    onPage={page => updateHistory({ page, size: history.data?.page_size ?? history.size, anchor: history.data?.anchor, lookup: undefined })}
    onJump={page => updateHistory({ page, size: history.data?.page_size ?? history.size, anchor: history.data?.anchor, lookup: undefined })}
    onSize={setPageSize} />

  return <div className="page-stack models-page">
    <header className="page-heading section-heading"><div><Link to="/models">全部模型</Link><h1>{model?.name || '模型详情'}</h1></div><button onClick={() => setRefresh(value => value + 1)} disabled={busy}>刷新模型</button></header>
    {error && <p role="alert" className="error callout">{error}</p>}{notice && <p role="status" className="callout success">{notice}</p>}{!model && !error && <p>加载模型…</p>}
    {model && <>
      <CurrentModelSnapshot model={model} />
      <section className="panel"><h2>标的组合</h2>
        <p className="model-basket">{model.framework_binding?.ordered_pairs.map(pair => `${pair.instrument_id.exchange}:${pair.instrument_id.code}`).join(' → ')}</p>
      </section>
      <section className="panel"><h2>固定研究条件</h2><p className="model-basket">{model.framework_binding ? model.framework_binding.ordered_pairs.map(pair => pair.local_symbol).join(' → ') : model.spec.backtest.symbols.join(' → ')}</p>
        <p>{model.spec.frequency === 'weekly' ? '自然周' : '自然月'} 决策 · 已训练 {model.total_timesteps.toLocaleString('zh-CN')} 步</p>
        <p>训练 {model.spec.train_start}–{model.spec.train_end} · 验证 {model.spec.validation_start}–{model.spec.validation_end} · 留出测试 {model.spec.test_start && model.spec.test_end ? `${model.spec.test_start}–${model.spec.test_end}` : '未设置留出测试期'}</p>
        <p>选模：{model.spec.selection_metric === 'validation_sharpe' ? '验证夏普率' : '验证净对数收益'}</p>
        <p>奖励目标：{rewardLabel(model.spec.reward_version)}；奖励值与回测净收益分别记录。</p>
        <p className="muted">篮子顺序、观察窗口、网络、日期、成本和风控均已固定。训练过程不自动评估测试段。</p><JsonDetails title="完整固定配置" value={model.spec} />
      </section>
      <section className="panel"><h2>{model.version_count ? '追加训练' : '开始训练'}</h2>{!model.version_count && <p className="empty">待训练：尚无保存版本。首次训练成功后才能回测。</p>}
        <form noValidate onSubmit={event => { event.preventDefault(); void submitTraining() }}><fieldset disabled={busy || !!activeId}><div className="form-grid">
          <label>追加决策步数<input type="number" min={2} max={10000000} step={1} value={budget} onChange={event => setBudget(Number(event.target.value))} /></label>
          <label>下次提交设备<select value={device} onChange={event => setDevice(event.target.value as 'cpu' | 'cuda')}><option value="cpu">CPU</option><option value="cuda" disabled={!cudaAvailable}>CUDA</option></select></label>
        </div><p className="muted">设备选择仅用于下次提交；每次追加有步数上限，成功后可继续追加。续训从上次成功版本的最终参数和优化器状态恢复。</p>
        <button type="button" disabled={!model.framework_binding} onClick={() => void checkTraining()}>检查续训能力</button>
        <button disabled={validTrainReport?.status !== 'ready'}>{model.version_count ? '继续训练' : '开始训练'}</button></fieldset></form>
        {validTrainReport && <CapabilityReportView report={validTrainReport} />}
        {activeId && <div className="callout"><p>当前会话 {activeId} · {model.active_session ? stateLabels[model.active_session.state] || model.active_session.state : '已提交，状态待刷新'}</p><p>取消会丢弃本次未发布进度，已有版本继续可用。</p><button disabled={busy || model.active_session?.state === 'cancel_requested'} onClick={() => void cancelTraining()}>取消当前训练</button></div>}
      </section>
      <section className="panel" aria-label="训练历史"><div className="section-heading"><div><h2>训练历史 · {history.data?.total ?? model.session_count} 次尝试</h2><p className="muted">成功训练显示本轮最终参数的验证成绩 · {model.spec.validation_start}–{model.spec.validation_end}。最优快照按{model.spec.selection_metric === 'validation_sharpe' ? '验证夏普率' : '验证净对数收益'}选定，回测使用该快照；续训使用本轮最终参数。采样日期显示本轮实际使用片段的最早与最晚日期，片段可能重叠或不连续。</p></div></div>
        {newCount > 0 && <p role="status" className="new-history-notice">有{newCount}条新记录，<button onClick={showLatest}>查看最新</button></p>}
        <form className="history-search" onSubmit={event => { event.preventDefault(); lookupRound(roundInput) }}><label>查找训练轮次<input inputMode="numeric" placeholder="例如 53" value={roundInput} onChange={event => setRoundInput(event.target.value)} /></label><button disabled={history.loading}>查找轮次</button></form>
        {renderPagination()}
        {history.error && <p role="alert" className="error">{history.error} <button disabled={history.loading} onClick={() => updateHistory({})}>重试加载</button></p>}{history.loading && <p>加载训练历史…</p>}
        {!history.loading && !history.error && !history.data?.items.length && <p className="empty">尚未提交训练。</p>}
        <div className="record-list">{history.data?.items.map(savedItem => {
          const item = model.active_session?.id === savedItem.id ? model.active_session : savedItem
          const artifact = artifacts[item.id], manifest = artifact?.manifest || {}
          const samplingIntervals = trainingSamplingIntervals(manifest)
          return <article className="record history-record" id={`history-${item.id}`} tabIndex={-1} key={item.id}>
            {item.round_number !== null ? <>
              <div className="record-head"><strong>第{item.round_number}轮</strong><span className={`badge snapshot-status${item.selection_valid === true && item.best_round_number === item.round_number ? ' updated' : ''}`}>{snapshotStatus(item)}</span><Link to={`/runs?model=${encodeURIComponent(id)}&version=${encodeURIComponent(item.id)}`}>使用此版本回测</Link><button aria-expanded={expanded === item.id} onClick={() => setExpanded(expanded === item.id ? '' : item.id)}>{expanded === item.id ? '收起详情' : '展开详情'}</button><button onClick={() => { focusTarget.current = 'training-log'; setSelectedLog(item) }}>查看日志</button></div>
              <TrainingSamplingLabel item={item} />
              <dl className="history-key-metrics" aria-label="本轮验证成绩">{primaryMetrics.map(([key, label]) => <div key={key}><dt id={`metric-${item.id}-${key}`}>{label}</dt><dd aria-labelledby={`metric-${item.id}-${key}`}>{metricText(key, item.validation_latest?.[key])}</dd></div>)}</dl>
              {item.selection_valid === false && <p className="callout warn">无有效验证评分：本版本保留了可用策略快照。</p>}
              {expanded === item.id && <div className="history-detail">
              <p className="muted">保存时间 {taskTime(item.saved_at || item.created_at)} · 累计步数 {String(item.timesteps_completed ?? '—')} · 实际设备 {item.runtime_device === 'cuda' ? 'CUDA（GPU）' : item.runtime_device === 'cpu' ? 'CPU' : '—'}</p>
              <p className="muted">第{item.session_number}次训练尝试 · 基础版本 {item.base_round_number ? `第${item.base_round_number}轮` : item.base_version_id || '首次初始化'}</p>
              <p className="muted">以下指标的验证区间：{model.spec.validation_start}–{model.spec.validation_end}</p>
              <h3>本轮最终参数验证</h3><Metrics values={item.validation_latest || {}} /><h3>选定快照验证</h3><Metrics values={item.validation_best || {}} />
              <p>选定快照来源：{item.best_round_number ? `第${item.best_round_number}轮` : item.best_source_version_id || '—'} · 选定快照步数 {item.best_timesteps ?? '—'}</p>
              <details><summary>版本诊断</summary><p>{item.id}</p></details>
              {artifactLoading === item.id && <p role="status">加载版本完整信息…</p>}
              {artifactErrors[item.id] && <p role="alert" className="error">{artifactErrors[item.id]} <button onClick={() => setArtifactRetry(value => value + 1)}>重试详情</button></p>}
              {artifact && <>
                {samplingIntervals.length > 0 && <details><summary>实际采样片段</summary><ol>{samplingIntervals.map((interval, index) => <li key={`${interval.start}-${interval.end}-${index}`}>{interval.start}–{interval.end}</li>)}</ol></details>}
                <JsonDetails title="验证评估记录与版本完整信息" value={manifest} />
              </>}
              </div>}
            </> : <>
              <div className="record-head"><strong>第{item.session_number}次训练尝试</strong><span className="badge">{stateLabels[item.state] || item.state}</span><span>{taskTime(item.created_at)}</span><button onClick={() => { focusTarget.current = 'training-log'; setSelectedLog(item) }}>查看日志</button></div>
              <TrainingSamplingLabel item={item} />
              <p className="muted">基础版本 {item.base_round_number ? `第${item.base_round_number}轮` : item.base_version_id || '首次初始化'} · 未保存新版本</p>
              <details><summary>{item.error ? `失败或中断原因：${item.error.slice(0, 120)}` : '会话详情'}</summary><p>进度 {item.completed_through || '—'} · 心跳 {taskTime(item.heartbeat)}</p><p className="muted">任务 ID {item.id}</p>{item.error && <p className="error">{item.error}</p>}</details>
            </>}
          </article>
        })}</div>{renderPagination(true)}
      </section>
      {selectedLog && <section className="panel" id="training-log" tabIndex={-1} aria-label="训练日志"><div className="section-heading"><h2>第{selectedLog.session_number}次尝试 · 训练日志</h2><button onClick={() => setSelectedLog(null)}>关闭日志</button></div>
        {logError && <p role="alert" className="error">{logError}</p>}<ol className="event-timeline">{logs.map(event => <li key={event.seq}><small>{event.phase}</small><p>{event.message}</p>{typeof event.payload.progress === 'number' && <progress aria-label="训练会话进度" max={1} value={event.payload.progress} />}{event.payload.timesteps !== undefined && <small>累计步数 {String(event.payload.timesteps)}</small>}{event.payload.validation_sharpe !== undefined && <small> 验证夏普 {String(event.payload.validation_sharpe ?? '—')}</small>}</li>)}</ol><p className="muted">保留最近 500 条事件。</p>
      </section>}
    </>}
  </div>
}

function rewardLabel(rewardVersion: string | undefined) {
  switch (rewardVersion ?? 'net-log-return-v1') {
    case 'net-log-return-v1': return '净对数收益（现有）'
    case 'net-log-return-with-state-v1': return '净对数收益 + 状态输入（研究对照）'
    case 'differential-sharpe-v1': return 'Differential Sharpe Ratio（DSR）'
    default: return rewardVersion
  }
}
