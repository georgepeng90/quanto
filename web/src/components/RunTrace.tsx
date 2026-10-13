import { useEffect, useState } from 'react'
import type { TableRow } from '../types'
import { cell, useTablePage } from './ResultTables'
import { ResultLoadNotice } from './ResultLoadNotice'

type DetailProps = { runId: string; defaultDate: string; startDate?: string; endDate?: string; refreshToken?: number; live?: boolean }
type TracePage = ReturnType<typeof useTablePage>
const text = (value: unknown) => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)

function TraceSection({ label, data, renderRow }: { label: string; data: TracePage; renderRow: (row: TableRow, index: number) => React.ReactNode }) {
  const rows = data.page?.items || []
  const start = data.index * 100 + (rows.length ? 1 : 0)
  const end = data.index * 100 + rows.length
  return <section role="region" aria-label={`${label}追踪`}><h3>{label}</h3>
    <ResultLoadNotice state={data} label={label} onRetry={data.retry} />
    {rows.length > 0 && <div className="record-list">{rows.map(renderRow)}</div>}
    {!data.loading && !data.error && data.page && rows.length === 0 && <p className="empty">该筛选下无{label}。</p>}
    {data.page && <div className="inline-actions"><button type="button" disabled={data.loading || data.index === 0} onClick={data.previous}>上一页</button><span>第 {start}–{end} 行 / 共 {data.page.total} 行</span><button type="button" disabled={data.loading || !data.page.next_cursor} onClick={data.next}>下一页</button></div>}
  </section>
}

function TraceDay({ runId, day, symbol, refreshToken, live }: { runId: string; day: string; symbol: string; refreshToken?: number; live?: boolean }) {
  const filters = { context_date: day, symbol }
  const signals = useTablePage(runId, 'signals', filters, refreshToken, live)
  const risks = useTablePage(runId, 'risk_events', filters, refreshToken, live)
  const orders = useTablePage(runId, 'orders', filters, refreshToken, live)
  const fills = useTablePage(runId, 'fills', filters, refreshToken, live)
  const visibleSignalDates = new Set((signals.page?.items || []).map(row => String(row.date)))
  const visibleOrderIds = new Set((orders.page?.items || []).map(row => String(row.id)))
  return <>
    <TraceSection label="策略信号" data={signals} renderRow={(row, index) => <article className="record" id={`signal-${row.date}`} key={`${row.date}-${index}`}><strong>{text(row.date)} · <span title={text(row.intent)}>{cell(row.intent, 'intent')}</span></strong>{day && row.date !== day && <p className="muted">可见订单的信号日上下文。</p>}{row.approved_intent == null && <p className="callout">历史记录未保存原始信号；以下为当时已记录目标，不能证明是风控前策略意图。</p>}<p>{row.approved_intent == null ? '历史已记录目标权重' : '策略原始目标权重'} {text(row.weights)}</p><p>原因 {text(row.reasons)}</p><details><summary>策略诊断</summary><pre>{text(row.diagnostics)}</pre></details>{row.approved_intent != null && <><p>风控批准意图 {cell(row.approved_intent, 'intent')}</p><p>风控批准目标权重 {text(row.approved_weights)}</p><p>批准原因 {text(row.approved_reasons)}</p></>}</article>} />
    <TraceSection label="风控事件" data={risks} renderRow={(row, index) => <article className="record" key={`${row.date}-${index}`}><strong>{text(row.date)} · {text(row.code)}</strong>{day && row.date !== day && <p className="muted">可见订单的信号日上下文；不是订单直接关联的风控决定。</p>}<p>{text(row.message)}</p><pre>{text(row.details)}</pre></article>} />
    <TraceSection label="订单" data={orders} renderRow={(row, index) => <article className="record" id={`order-${row.id}`} key={`${row.id}-${index}`}><strong>{text(row.signal_date)} · {text(row.symbol)} · <span title={text(row.side)}>{cell(row.side, 'side')}</span> · <span title={text(row.status)}>{cell(row.status, 'status')}</span></strong><p>订单 {text(row.id)} · 来源 <span title={text(row.source)}>{cell(row.source, 'source')}</span> · 原因 {text(row.reason)}</p><p>当日信号：{visibleSignalDates.has(String(row.signal_date)) ? <a href={`#signal-${row.signal_date}`}>{text(row.signal_date)}</a> : <span>{text(row.signal_date)}（当前信号页未显示，可查询该信号日）</span>}（日期上下文）</p><p>委托 {text(row.quantity)} · 已成交 {text(row.filled_quantity)} · 到期未成交 {text(row.expired_quantity)} · 执行日 {text(row.execution_date)}</p></article>} />
    <TraceSection label="成交" data={fills} renderRow={(row, index) => <article className="record" key={`${row.order_id}-${row.trade_date}-${index}`}><strong>{text(row.trade_date)} · {text(row.symbol)} · <span title={text(row.side)}>{cell(row.side, 'side')}</span></strong><p>数量 {text(row.quantity)} · 价格 {text(row.price)} · 佣金 {text(row.commission)} · 印花税 {text(row.stamp_duty)} · 过户费 {text(row.transfer_fee)} · 滑点成本 {text(row.slippage_cost)}</p><p>订单：{visibleOrderIds.has(String(row.order_id)) ? <a href={`#order-${row.order_id}`}>{text(row.order_id)}</a> : <span>{text(row.order_id)}（当前订单页未显示）</span>}</p></article>} />
  </>
}

export function RunTrace({ runId, defaultDate, startDate, endDate, refreshToken, live }: DetailProps) {
  const [custom, setCustom] = useState(false)
  const [day, setDay] = useState(defaultDate)
  const [draftDay, setDraftDay] = useState(defaultDate)
  const [symbol, setSymbol] = useState('')
  const [draftSymbol, setDraftSymbol] = useState('')
  useEffect(() => { if (!custom) { setDay(defaultDate); setDraftDay(defaultDate) } }, [custom, defaultDate])
  const inRange = (value: string) => !!value && (!startDate || value >= startDate) && (!endDate || value <= endDate)
  const adjacentDay = (offset: number) => {
    if (!inRange(draftDay)) return ''
    const date = new Date(`${draftDay}T00:00:00Z`)
    if (!Number.isFinite(date.getTime())) return ''
    date.setUTCDate(date.getUTCDate() + offset)
    const value = date.toISOString().slice(0, 10)
    return inRange(value) ? value : ''
  }
  const previousDay = adjacentDay(-1)
  const nextDay = adjacentDay(1)
  const apply = (value = draftDay) => {
    if (!inRange(value)) return
    setCustom(true)
    setDay(value)
    setDraftDay(value)
    setSymbol(draftSymbol.trim())
  }
  return <section className="panel"><h2>交易决策追踪</h2><p className="muted">成交通过 order_id 精确关联订单；信号与风控由服务端按日期提供上下文。每类记录单独分页，每页最多 100 行。</p>
    <form className="form-grid" onSubmit={event => { event.preventDefault(); apply() }}><label>模拟日期<input type="date" required min={startDate} max={endDate} value={draftDay} onChange={event => setDraftDay(event.target.value)} /></label><label>标的<input type="text" value={draftSymbol} onChange={event => setDraftSymbol(event.target.value)} placeholder="可选" /></label><button type="submit">查询追踪</button></form>
    <div className="inline-actions"><button type="button" disabled={!previousDay} onClick={() => apply(previousDay)}>前一天</button><button type="button" disabled={!nextDay} onClick={() => apply(nextDay)}>后一天</button><span className="muted">按自然日切换并直接查询。</span></div>
    <TraceDay key={JSON.stringify([runId, day, symbol])} runId={runId} day={day} symbol={symbol} refreshToken={refreshToken} live={live} />
  </section>
}
