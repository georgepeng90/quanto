import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { DataDisclosure, metricValue } from '../components/metricPresentation'
import { api, ApiError, describeError } from '../api'
import { FrameworkProvenance } from '../components/FrameworkProvenance'
import { EventTimeline } from '../components/EventTimeline'
import { ResultCharts } from '../components/ResultCharts'
import { ResultTables } from '../components/ResultTables'
import { RunTrace } from '../components/RunTrace'
import { RunProgress, terminalStates } from '../components/RunProgress'
import type { RunEvent, RunRecord, RunResult } from '../types'

const text = (value: unknown) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)
const metricLabels: Record<string, string> = { final_equity: '最终权益', total_return: '累计收益', annual_return: '年化收益', volatility: '波动率', sharpe: '夏普比率', max_drawdown: '最大回撤', calmar: 'Calmar 比率', fill_count: '成交笔数', round_trip_count: '完整往返次数', open_lot_count: '未平仓批次', realized_pnl: '已实现盈亏', unrealized_pnl: '未实现盈亏', turnover: '换手', commission_paid: '佣金', slippage_cost: '滑点成本', execution_cost: '执行成本', average_invested_ratio: '平均持仓比例', average_holding_sessions: '平均持有交易日', dividend_income: '分红收入', interest_income: '利息收入', benchmark_return: '基准收益', relative_return_difference: '相对收益差' }
const modeLabel = (mode: unknown) => mode === 'raw_cash' ? '原始现金模式' : mode === 'adjusted_research' ? '复权研究模式' : String(mode || '未知模式')
const retryable = (error: unknown) => !(error instanceof ApiError) || error.status === 408 || error.status === 429 || error.status >= 500

export function RunDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const [cloneBusy, setCloneBusy] = useState(false)
  const [run, setRun] = useState<RunRecord | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [refreshToken, setRefreshToken] = useState(0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [result, setResult] = useState<RunResult | null>(null)
  const [downloadBusy, setDownloadBusy] = useState(false)
  const resultRef = useRef<RunResult | null>(null)
  const inFlight = useRef<AbortSignal | null>(null)
  const refreshFailures = useRef(0)
  const retryPending = useRef(true)
  const runRef = useRef<RunRecord | null>(null)
  const mountController = useRef<AbortController | null>(null)

  const refresh = useCallback(async (signal: AbortSignal, force = false) => {
    if (signal.aborted || (inFlight.current && !inFlight.current.aborted) || !id) return
    inFlight.current = signal
    setLoading(true)
    retryPending.current = false
    if (force) { refreshFailures.current = 0; resultRef.current = null }
    try {
      const current = await api.run(id, signal)
      if (signal.aborted) return
      const previous = runRef.current
      const completed_through = previous?.completed_through && (!current.completed_through || previous.completed_through > current.completed_through) ? previous.completed_through : current.completed_through
      const normalized = { ...current, completed_through }
      runRef.current = normalized
      setRun(normalized)
      const [logs, final] = await Promise.allSettled([
        api.runLogs(id, undefined, signal, true),
        current.state === 'succeeded' && !resultRef.current ? api.runResult(id, signal) : Promise.resolve(resultRef.current),
      ])
      if (signal.aborted) return
      const failures: unknown[] = []
      if (logs.status === 'fulfilled') setEvents(logs.value.items.slice(-100))
      else failures.push(logs.reason)
      if (final.status === 'fulfilled') { resultRef.current = final.value; setResult(final.value) }
      else failures.push(final.reason)
      setError(failures.map(describeError).join('；'))
      if (failures.length) retryPending.current = failures.some(retryable) && ++refreshFailures.current < 3
      else refreshFailures.current = 0
    } catch (value) {
      if (!signal.aborted) {
        setError(describeError(value))
        retryPending.current = retryable(value) && ++refreshFailures.current < 3
      }
    } finally {
      if (inFlight.current === signal) { inFlight.current = null; if (!signal.aborted) setLoading(false) }
    }
  }, [id])

  useEffect(() => {
    const controller = new AbortController()
    mountController.current = controller
    void refresh(controller.signal)
    const timer = window.setInterval(() => { if (!terminalStates.has(runRef.current?.state || 'queued') || retryPending.current) void refresh(controller.signal) }, 1000)
    return () => { clearInterval(timer); controller.abort(); mountController.current = null }
  }, [refresh])

  const cancel = async () => {
    if (!run || terminalStates.has(run.state)) return
    const signal = mountController.current?.signal
    try {
      const current = await api.cancelRun(id)
      if (signal?.aborted) return
      runRef.current = current
      setRun(current)
      if (signal) await refresh(signal)
    } catch (value) { if (!signal?.aborted) { setError(describeError(value)); if (signal) await refresh(signal) } }
  }
  const download = async (format: 'csv' | 'json' | 'html') => {
    if (downloadBusy) return
    const signal = mountController.current?.signal
    setDownloadBusy(true)
    try {
      const blob = await api.exportRun(id, format, signal)
      if (signal?.aborted) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `回测-${id}-${format}.${format === 'html' ? 'html' : 'zip'}`
      document.body.append(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (value) { if (!signal?.aborted) setError(describeError(value)) }
    finally { if (!signal?.aborted) setDownloadBusy(false) }
  }
  const clone = async () => {
    if (cloneBusy) return
    setCloneBusy(true)
    const signal = mountController.current?.signal
    try { const draft = await api.cloneRun(id); if (!signal?.aborted) navigate(`/experiments/${encodeURIComponent(draft.id)}`) }
    catch (value) { if (!signal?.aborted) setError(describeError(value)) }
    finally { if (!signal?.aborted) setCloneBusy(false) }
  }
  const latest = events.at(-1)
  const progress = events.filter(event => event.total_days > 0).at(-1)
  const startDate = String(run?.provenance?.start || '')
  const endDate = String(run?.provenance?.end || '')
  const defaultDate = run?.completed_through || startDate
  const refreshAll = () => { setRefreshToken(value => value + 1); if (mountController.current) void refresh(mountController.current.signal, true) }
  return <div className="page-stack"><header className="page-heading"><h1>回测详情</h1><p><Link to="/runs">返回运行列表</Link></p></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    {loading && !run ? <p>读取运行状态…</p> : !run ? <p className="empty">尚无法读取运行详情。<button type="button" onClick={refreshAll}>刷新重试</button></p> : <>
      <section className="panel"><div className="section-heading"><div><h2>运行进度</h2><p>任务和时间以服务端记录为准。</p></div><div className="inline-actions"><button type="button" disabled={loading} onClick={refreshAll}>刷新状态</button><button type="button" disabled={terminalStates.has(run.state) || run.state === 'cancel_requested'} onClick={cancel}>{run.state === 'cancel_requested' ? '正在取消' : '取消任务'}</button></div></div><RunProgress run={run} latest={latest} progress={progress} />{run.error && <p className="error callout">{run.error}</p>}</section>
      <section className="panel"><h2>回测条件</h2><button type="button" disabled={cloneBusy} onClick={clone}>从此回测创建固定策略</button><FrameworkProvenance value={result?.metadata?.framework ? result.metadata : run.provenance} /><details><summary>诊断信息</summary><pre>{JSON.stringify({ experiment_id: run.experiment_id, experiment_revision: run.experiment_revision, manifest_hash: run.manifest_hash, provenance: run.provenance }, null, 2)}</pre></details></section>
      <section className="panel"><h2>任务日志</h2><p className="muted">展示最近 100 条事件。</p>{events.length ? <EventTimeline events={events} /> : <p className="empty">尚无日志事件。</p>}</section>
      {run.state === 'succeeded' && result?.metrics && <section className="panel"><h2>最终绩效</h2><DataDisclosure provenance={run.provenance} /><p><span>实际持有 {Array.isArray(result.held_symbols) ? result.held_symbols.length : '待服务端确认'} 只</span> · <span>回测范围 {Array.isArray(run.provenance?.symbols) ? run.provenance.symbols.length : '未知'} 只</span></p>{Array.isArray(result.held_symbols) && <details><summary>回测期间持有标的</summary>{result.held_symbols.length ? <p>{result.held_symbols.join('、')}</p> : <p>回测期间未持有标的</p>}</details>}<p>{modeLabel(result.metadata?.price_mode || run.provenance?.price_mode)}</p><p className="muted">指标和定义来自服务端完整报告；不可用值按原样显示，不据可视范围重算。</p><div className="metric-grid">{Object.entries(result.metrics).map(([key, value]) => <div className="metric" key={key}><strong>{metricLabels[key] || key}</strong><span title={String(value ?? "")}>{metricValue(key, value, result.metadata?.currency || run.provenance?.currency)}</span>{result.metric_definitions[key]?.description != null && <small>{text(result.metric_definitions[key].description)}</small>}</div>)}</div>{result.warnings?.map((warning, index) => <p className="callout" key={index}>{warning}</p>)}<details><summary>完整指标定义与来源</summary><pre>{JSON.stringify({ metrics: result.metrics, metric_definitions: result.metric_definitions, metadata: result.metadata }, null, 2)}</pre></details><div className="inline-actions"><button type="button" disabled={downloadBusy} onClick={() => void download('csv')}>CSV 明细 ZIP</button><button type="button" disabled={downloadBusy} onClick={() => void download('json')}>JSON 报告 ZIP</button><button type="button" disabled={downloadBusy} onClick={() => void download('html')}>独立 HTML</button></div><p className="muted">下载来自服务端已校验产物，包含数据版本与数据模式；ZIP 内清单描述完整报告，可能列出当前子集包之外的文件。</p></section>}
      {run.state !== 'succeeded' && <section className="panel"><h2>账户净值 · 部分结果</h2><p className="muted">下列数值仅为已完成交易日的服务端产物；最终绩效不可用。</p></section>}
      {run.state === 'succeeded' && <ResultCharts runId={id} startDate={startDate} endDate={endDate} refreshToken={refreshToken} />}
      <ResultTables runId={id} defaultDate={defaultDate} startDate={startDate} endDate={endDate} refreshToken={refreshToken} live={!terminalStates.has(run.state)} />
      <RunTrace runId={id} defaultDate={defaultDate} startDate={startDate} endDate={endDate} refreshToken={refreshToken} live={!terminalStates.has(run.state)} />
    </>}
  </div>
}
