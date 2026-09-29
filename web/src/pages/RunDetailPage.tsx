import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { EventTimeline } from '../components/EventTimeline'
import { RunProgress, terminalStates } from '../components/RunProgress'
import type { RunEvent, RunRecord, TableName, TableRow } from '../types'

const tableNames: TableName[] = ['equity', 'orders', 'fills', 'signals', 'risk_events', 'account_events']
const emptyTables = (): Record<TableName, TableRow[]> => ({ equity: [], orders: [], fills: [], signals: [], risk_events: [], account_events: [] })
const text = (value: unknown) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)
const rowDate = (row: TableRow) => String(row.date || row.signal_date || row.trade_date || '')

export function RunDetailPage() {
  const { id = '' } = useParams()
  const [run, setRun] = useState<RunRecord | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [tables, setTables] = useState<Record<TableName, TableRow[]>>(emptyTables)
  const [date, setDate] = useState('')
  const [symbol, setSymbol] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const logCursor = useRef<string | undefined>(undefined)
  const tableCursors = useRef<Record<TableName, string | undefined>>({ equity: undefined, orders: undefined, fills: undefined, signals: undefined, risk_events: undefined, account_events: undefined })
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
      setEvents(old => {
        const bySeq = new Map(old.map(event => [event.seq, event]))
        for (const item of logs.items) bySeq.set(item.seq, item)
        return [...bySeq.values()].sort((a, b) => a.seq - b.seq)
      })
      logCursor.current = logs.cursor
      const loaded = await Promise.all(tableNames.map(async name => {
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
  const latest = events.at(-1)
  const progress = events.filter(event => event.total_days > 0).at(-1)
  const orderById = new Map(tables.orders.map(row => [String(row.id), row]))
  const matchesDate = (name: TableName, row: TableRow) => !date || rowDate(row) === date || (name === 'orders' && row.execution_date === date) || (name === 'fills' && orderById.get(String(row.order_id))?.signal_date === date)
  const filtered = (name: TableName) => tables[name].filter(row => matchesDate(name, row) && (!symbol || name === 'signals' || name === 'risk_events' || String(row.symbol || '') === symbol))
  const orders = filtered('orders')
  const fills = filtered('fills')
  const signals = filtered('signals')
  const risks = filtered('risk_events')
  const dates = [...new Set([...tables.signals, ...tables.risk_events, ...tables.orders, ...tables.fills].map(rowDate).filter(Boolean))].sort()
  const symbols = [...new Set([...tables.orders, ...tables.fills].map(row => String(row.symbol || '')).filter(Boolean))].sort()
  return <div className="page-stack"><header className="page-heading"><h1>回测详情</h1><p><Link to="/runs">返回运行列表</Link> · {id}</p></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    {loading && !run ? <p>读取运行状态…</p> : !run ? <p className="empty">尚无法读取运行详情。可刷新重试。</p> : <>
      <section className="panel"><div className="section-heading"><div><h2>运行进度</h2><p>任务和时间以服务端记录为准。</p></div><div className="inline-actions"><button type="button" onClick={() => void refresh(mountController.current?.signal)}>刷新状态</button><button type="button" disabled={terminalStates.has(run.state) || run.state === 'cancel_requested'} onClick={cancel}>{run.state === 'cancel_requested' ? '正在取消' : '取消任务'}</button></div></div><RunProgress run={run} latest={latest} progress={progress} />{run.error && <p className="error callout">{run.error}</p>}</section>
      <section className="panel"><h2>输入快照</h2><p>实验 {run.experiment_id} · 修订 {run.experiment_revision} · 清单哈希 {run.manifest_hash}</p><details><summary>查看冻结输入与来源</summary><pre>{JSON.stringify(run.provenance || {}, null, 2)}</pre></details></section>
      <section className="panel"><h2>任务日志</h2>{events.length ? <EventTimeline events={events} /> : <p className="empty">尚无日志事件。</p>}</section>
      <section className="panel"><h2>账户净值 · {run.state === 'succeeded' ? '已完成' : `${run.state === 'running' ? '运行中' : '部分结果'}`}</h2><p className="muted">下列数值为已完成交易日的服务端产物；不据此显示最终绩效排名。</p>{tables.equity.length ? <div className="table-wrap"><table><thead><tr><th>日期</th><th>现金</th><th>应收</th><th>市值</th><th>净值</th><th>陈旧估值</th></tr></thead><tbody>{tables.equity.map((row, index) => <tr key={`${row.date}-${index}`}><td>{text(row.date)}</td><td>{text(row.cash)}</td><td>{text(row.receivables)}</td><td>{text(row.market_value)}</td><td>{text(row.equity)}</td><td>{text(row.stale_value)}</td></tr>)}</tbody></table></div> : <p className="empty">尚无已完成日净值。</p>}</section>
      <section className="panel"><h2>交易决策追踪</h2><p className="muted">成交通过 order_id 精确关联订单；信号与风控仅按日期提供上下文。</p><div className="form-grid"><label>模拟日期<select value={date} onChange={event => setDate(event.target.value)}><option value="">全部日期</option>{dates.map(value => <option key={value} value={value}>{value}</option>)}</select></label><label>标的<select value={symbol} onChange={event => setSymbol(event.target.value)}><option value="">全部标的</option>{symbols.map(value => <option key={value} value={value}>{value}</option>)}</select></label></div>
        <h3>策略信号</h3>{signals.length ? signals.map((row, index) => <article className="record" key={`${row.date}-${index}`}><strong>{text(row.date)} · {text(row.intent)}</strong><p>目标权重 {text(row.weights)}</p><p>原因 {text(row.reasons)}</p><details><summary>策略诊断</summary><pre>{text(row.diagnostics)}</pre></details></article>) : <p className="empty">该筛选下无策略信号。</p>}
        <h3>当日风控上下文</h3>{risks.length ? risks.map((row, index) => <article className="record" key={`${row.date}-${index}`}><strong>{text(row.date)} · {text(row.code)}</strong><p>{text(row.message)}</p><pre>{text(row.details)}</pre></article>) : <p className="empty">该筛选下无风控事件。</p>}
        <h3>订单</h3>{orders.length ? <div className="record-list">{orders.map(row => <article className="record" id={`order-${row.id}`} key={String(row.id)}><strong>{text(row.signal_date)} · {text(row.symbol)} · {text(row.side)} · {text(row.status)}</strong><p>订单 {text(row.id)} · 来源 {text(row.source)} · 原因 {text(row.reason)}</p><p>委托 {text(row.quantity)} · 已成交 {text(row.filled_quantity)} · 到期未成交 {text(row.expired_quantity)} · 执行日 {text(row.execution_date)}</p>{!tables.fills.some(fill => fill.order_id === row.id) && <p>无成交：{text(row.reason)}</p>}</article>)}</div> : <p className="empty">该筛选下无订单。</p>}
        <h3>成交</h3>{fills.length ? <div className="record-list">{fills.map((row, index) => <article className="record" key={`${row.order_id}-${row.trade_date}-${index}`}><strong>{text(row.trade_date)} · {text(row.symbol)} · {text(row.side)}</strong><p>数量 {text(row.quantity)} · 价格 {text(row.price)} · 佣金 {text(row.commission)} · 滑点成本 {text(row.slippage_cost)}</p><p>订单：{orderById.has(String(row.order_id)) ? <a href={`#order-${row.order_id}`}>{text(row.order_id)}</a> : text(row.order_id)}</p></article>)}</div> : <p className="empty">该筛选下无成交；查看订单原因。</p>}</section>
    </>}
  </div>
}
