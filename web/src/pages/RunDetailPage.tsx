import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { EventTimeline } from '../components/EventTimeline'
import { ResultCharts } from '../components/ResultCharts'
import { cell, ResultTables } from '../components/ResultTables'
import { RunProgress, terminalStates } from '../components/RunProgress'
import type { RunEvent, RunRecord, RunResult, TableName, TableRow } from '../types'

const tableNames: TableName[] = ['equity', 'orders', 'fills', 'signals', 'risk_events', 'account_events']
const reportTables: TableName[] = ['positions', 'curves', 'monthly_returns', 'annual_returns', 'matched_trades', 'open_lots']
const emptyTables = (): Record<TableName, TableRow[]> => ({ equity: [], orders: [], fills: [], signals: [], risk_events: [], account_events: [], positions: [], curves: [], monthly_returns: [], annual_returns: [], matched_trades: [], open_lots: [] })
const text = (value: unknown) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)
const rowDate = (row: TableRow) => String(row.date || row.signal_date || row.trade_date || '')
const metricLabels: Record<string, string> = { final_equity: '最终权益', total_return: '累计收益', annual_return: '年化收益', volatility: '波动率', sharpe: '夏普比率', max_drawdown: '最大回撤', calmar: 'Calmar 比率', fill_count: '成交笔数', round_trip_count: '完整往返次数', open_lot_count: '未平仓批次', realized_pnl: '已实现盈亏', unrealized_pnl: '未实现盈亏', turnover: '换手', commission_paid: '佣金', slippage_cost: '滑点成本', execution_cost: '执行成本', average_invested_ratio: '平均持仓比例', average_holding_sessions: '平均持有交易日', dividend_income: '分红收入', interest_income: '利息收入', benchmark_return: '基准收益', relative_return_difference: '相对收益差' }
const modeLabel = (mode: unknown) => mode === 'raw_cash' ? '原始现金模式' : mode === 'adjusted_research' ? '复权研究模式' : String(mode || '未知模式')

export function RunDetailPage() {
  const { id = '' } = useParams()
  const [run, setRun] = useState<RunRecord | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [tables, setTables] = useState<Record<TableName, TableRow[]>>(emptyTables)
  const [date, setDate] = useState('')
  const [symbol, setSymbol] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [result, setResult] = useState<RunResult | null>(null)
  const [downloadBusy, setDownloadBusy] = useState(false)
  const resultRef = useRef<RunResult | null>(null)
  const logCursor = useRef<string | undefined>(undefined)
  const tableCursors = useRef<Record<TableName, string | undefined>>(Object.fromEntries(Object.keys(emptyTables()).map(name => [name, undefined])) as Record<TableName, string | undefined>)
  const inFlight = useRef(false)
  const runRef = useRef<RunRecord | null>(null)
  const mountController = useRef<AbortController | null>(null)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (inFlight.current || !id) return
    inFlight.current = true
    try {
      const readLogs = async () => {
        const items: RunEvent[] = []
        let cursor = logCursor.current
        for (;;) {
          const page = await api.runLogs(id, cursor, signal)
          items.push(...page.items)
          if (page.items.length < 100 || !page.next_cursor || page.next_cursor === cursor) return { items, cursor: page.next_cursor || cursor }
          cursor = page.next_cursor
        }
      }
      const [current, logs] = await Promise.all([api.run(id, signal), readLogs()])
      if (signal?.aborted) return
      const previous = runRef.current
      const completed_through = previous?.completed_through && (!current.completed_through || previous.completed_through > current.completed_through) ? previous.completed_through : current.completed_through
      const normalized = { ...current, completed_through }
      runRef.current = normalized
      setRun(normalized)
      if (current.state === 'succeeded' && !resultRef.current) {
        const final = await api.runResult(id, signal)
        if (signal?.aborted) return
        resultRef.current = final
        setResult(final)
      }
      setEvents(old => {
        const bySeq = new Map(old.map(event => [event.seq, event]))
        for (const item of logs.items) bySeq.set(item.seq, item)
        return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
      })
      logCursor.current = logs.cursor
      const loaded = await Promise.all([...tableNames, ...(current.state === 'succeeded' ? reportTables : [])].map(async name => {
        const rows: TableRow[] = []
        let cursor = name === 'orders' || terminalStates.has(current.state) ? undefined : tableCursors.current[name]
        for (;;) {
          const page = await api.runTable(id, name, cursor, signal)
          rows.push(...page.items)
          if (!page.items.length || !page.next_cursor || page.next_cursor === cursor) {
            return { name, rows, cursor: page.next_cursor || cursor }
          }
          cursor = page.next_cursor
        }
      }))
      if (signal?.aborted) return
      setTables(old => {
        const next = terminalStates.has(current.state) ? emptyTables() : { ...old }
        for (const page of loaded) {
          if (page.name === 'orders') {
            const byId = new Map(next.orders.map(row => [String(row.id), row]))
            for (const row of page.rows) byId.set(String(row.id), row)
            next.orders = [...byId.values()]
          } else next[page.name] = [...next[page.name], ...page.rows]
          tableCursors.current[page.name] = page.cursor
        }
        return next
      })
      setError('')
    } catch (value) { if (!signal?.aborted) setError(describeError(value)) }
    finally { inFlight.current = false; if (!signal?.aborted) setLoading(false) }
  }, [id])

  useEffect(() => {
    const controller = new AbortController()
    mountController.current = controller
    void refresh(controller.signal)
    const timer = window.setInterval(() => { if (!terminalStates.has(runRef.current?.state || 'queued')) void refresh(controller.signal) }, 1000)
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
      await refresh(signal)
    } catch (value) { if (!signal?.aborted) { setError(describeError(value)); await refresh(signal) } }
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
  const latest = events.at(-1)
  const progress = events.filter(event => event.total_days > 0).at(-1)
  const orderById = new Map(tables.orders.map(row => [String(row.id), row]))
  const matchesDate = (name: TableName, row: TableRow) => !date || rowDate(row) === date || (name === 'orders' && row.execution_date === date) || (name === 'fills' && orderById.get(String(row.order_id))?.signal_date === date)
  const filtered = (name: TableName) => tables[name].filter(row => matchesDate(name, row) && (!symbol || name === 'signals' || name === 'risk_events' || String(row.symbol || '') === symbol))
  const orders = filtered('orders')
  const fills = filtered('fills')
  const signalDates = new Set(orders.map(row => String(row.signal_date || '')))
  const datedContext = (row: TableRow) => !date || rowDate(row) === date || signalDates.has(rowDate(row))
  const signals = tables.signals.filter(datedContext)
  const risks = tables.risk_events.filter(datedContext)
  const dates = [...new Set([...tables.signals, ...tables.risk_events, ...tables.orders, ...tables.fills].map(rowDate).filter(Boolean))].sort()
  const symbols = [...new Set([...tables.orders, ...tables.fills].map(row => String(row.symbol || '')).filter(Boolean))].sort()
  return <div className="page-stack"><header className="page-heading"><h1>回测详情</h1><p><Link to="/runs">返回运行列表</Link> · {id}</p></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    {loading && !run ? <p>读取运行状态…</p> : !run ? <p className="empty">尚无法读取运行详情。可刷新重试。</p> : <>
      <section className="panel"><div className="section-heading"><div><h2>运行进度</h2><p>任务和时间以服务端记录为准。</p></div><div className="inline-actions"><button type="button" onClick={() => void refresh(mountController.current?.signal)}>刷新状态</button><button type="button" disabled={terminalStates.has(run.state) || run.state === 'cancel_requested'} onClick={cancel}>{run.state === 'cancel_requested' ? '正在取消' : '取消任务'}</button></div></div><RunProgress run={run} latest={latest} progress={progress} />{run.error && <p className="error callout">{run.error}</p>}</section>
      <section className="panel"><h2>输入快照</h2><p>实验 {run.experiment_id} · 修订 {run.experiment_revision} · 清单哈希 {run.manifest_hash}</p><details><summary>查看冻结输入与来源</summary><pre>{JSON.stringify(run.provenance || {}, null, 2)}</pre></details></section>
      <section className="panel"><h2>任务日志</h2>{events.length ? <EventTimeline events={events} /> : <p className="empty">尚无日志事件。</p>}</section>
      {run.state === 'succeeded' && result?.metrics && <section className="panel"><h2>最终绩效</h2><p>数据版本 {text(result.metadata?.dataset_version_id || run.provenance?.dataset_version_id)} · 策略修订 {text(result.metadata?.strategy_revision_id || run.provenance?.strategy_revision_id)} · {modeLabel(result.metadata?.price_mode || run.provenance?.price_mode)}</p><p className="muted">指标和定义来自服务端完整报告；不可用值按原样显示，不据可视范围重算。</p><div className="metric-grid">{Object.entries(result.metrics).map(([key, value]) => <div className="metric" key={key}><strong>{metricLabels[key] || key}</strong><span>{value == null ? '不可用' : String(value)}</span>{result.metric_definitions[key]?.description != null && <small>{text(result.metric_definitions[key].description)}</small>}</div>)}</div>{result.warnings?.map((warning, index) => <p className="callout" key={index}>{warning}</p>)}<details><summary>完整指标定义与来源</summary><pre>{JSON.stringify({ metric_definitions: result.metric_definitions, metadata: result.metadata }, null, 2)}</pre></details><div className="inline-actions"><button type="button" disabled={downloadBusy} onClick={() => void download('csv')}>CSV 明细 ZIP</button><button type="button" disabled={downloadBusy} onClick={() => void download('json')}>JSON 报告 ZIP</button><button type="button" disabled={downloadBusy} onClick={() => void download('html')}>独立 HTML</button></div><p className="muted">下载来自服务端已校验产物，包含数据版本与数据模式；ZIP 内清单描述完整报告，可能列出当前子集包之外的文件。</p></section>}
      {run.state !== 'succeeded' && <section className="panel"><h2>账户净值 · 部分结果</h2><p className="muted">下列数值仅为已完成交易日的服务端产物；最终绩效不可用。</p></section>}
      {run.state === 'succeeded' && <ResultCharts curves={tables.curves} monthly={tables.monthly_returns} annual={tables.annual_returns} positions={tables.positions} />}
      <ResultTables tables={tables} />
      <section className="panel"><h2>交易决策追踪</h2><p className="muted">成交通过 order_id 精确关联订单；信号与风控仅按日期提供上下文。</p><div className="form-grid"><label>模拟日期<select value={date} onChange={event => setDate(event.target.value)}><option value="">全部日期</option>{dates.map(value => <option key={value} value={value}>{value}</option>)}</select></label><label>标的<select value={symbol} onChange={event => setSymbol(event.target.value)}><option value="">全部标的</option>{symbols.map(value => <option key={value} value={value}>{value}</option>)}</select></label></div>
        <h3>策略信号</h3>{signals.length ? signals.map((row, index) => <article className="record" id={`signal-${row.date}`} key={`${row.date}-${index}`}><strong>{text(row.date)} · <span title={text(row.intent)}>{cell(row.intent, 'intent')}</span></strong>{date && row.date !== date && <p className="muted">可见订单的信号日上下文</p>}<p>目标权重 {text(row.weights)}</p><p>原因 {text(row.reasons)}</p><details><summary>策略诊断</summary><pre>{text(row.diagnostics)}</pre></details></article>) : <p className="empty">该筛选下无策略信号。</p>}
        <h3>当日风控上下文</h3>{risks.length ? risks.map((row, index) => <article className="record" key={`${row.date}-${index}`}><strong>{text(row.date)} · {text(row.code)}</strong>{date && row.date !== date && <p className="muted">可见订单的信号日上下文；不是订单直接关联的风控决定。</p>}<p>{text(row.message)}</p><pre>{text(row.details)}</pre></article>) : <p className="empty">该筛选下无风控事件。</p>}
        <h3>订单</h3>{orders.length ? <div className="record-list">{orders.map(row => <article className="record" id={`order-${row.id}`} key={String(row.id)}><strong>{text(row.signal_date)} · {text(row.symbol)} · <span title={text(row.side)}>{cell(row.side, 'side')}</span> · <span title={text(row.status)}>{cell(row.status, 'status')}</span></strong><p>订单 {text(row.id)} · 来源 <span title={text(row.source)}>{cell(row.source, 'source')}</span> · 原因 {text(row.reason)}</p><p>当日信号：<a href={`#signal-${row.signal_date}`}>{text(row.signal_date)}</a>（日期上下文）</p><p>委托 {text(row.quantity)} · 已成交 {text(row.filled_quantity)} · 到期未成交 {text(row.expired_quantity)} · 执行日 {text(row.execution_date)}</p>{!tables.fills.some(fill => fill.order_id === row.id) && <p>无成交：{text(row.reason)}</p>}</article>)}</div> : <p className="empty">该筛选下无订单。</p>}
        <h3>成交</h3>{fills.length ? <div className="record-list">{fills.map((row, index) => <article className="record" key={`${row.order_id}-${row.trade_date}-${index}`}><strong>{text(row.trade_date)} · {text(row.symbol)} · <span title={text(row.side)}>{cell(row.side, 'side')}</span></strong><p>数量 {text(row.quantity)} · 价格 {text(row.price)} · 佣金 {text(row.commission)} · 滑点成本 {text(row.slippage_cost)}</p><p>订单：{orderById.has(String(row.order_id)) ? <a href={`#order-${row.order_id}`}>{text(row.order_id)}</a> : text(row.order_id)}</p></article>)}</div> : <p className="empty">该筛选下无成交；查看订单原因。</p>}</section>
    </>}
  </div>
}
