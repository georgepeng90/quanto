import { useEffect, useState } from 'react'
import { api, describeError } from '../api'
import type { TableName, TablePage, TableQuery, TableRow } from '../types'
import { ResultLoadNotice } from './ResultLoadNotice'

const definitions: { name: TableName; label: string; columns: [string, string][] }[] = [
  { name: 'equity', label: '账户', columns: [['date', '日期'], ['cash', '现金'], ['receivables', '应收'], ['market_value', '市值'], ['equity', '净值'], ['stale_value', '陈旧估值']] },
  { name: 'positions', label: '持仓', columns: [['date', '日期'], ['symbol', '代码'], ['shares', '份额'], ['sellable_shares', '可卖份额'], ['market_value', '市值'], ['weight', '权重'], ['cost_basis', '成本'], ['unrealized_pnl', '未实现盈亏'], ['realized_pnl', '已实现盈亏']] },
  { name: 'orders', label: '订单', columns: [['id', '订单'], ['symbol', '代码'], ['side', '方向'], ['quantity', '委托数量'], ['filled_quantity', '成交数量'], ['expired_quantity', '到期未成交'], ['status', '状态'], ['signal_date', '信号日'], ['execution_date', '执行日'], ['source', '来源'], ['reason', '原因']] },
  { name: 'fills', label: '成交与费用', columns: [['order_id', '订单'], ['trade_date', '成交日'], ['symbol', '代码'], ['side', '方向'], ['quantity', '数量'], ['price', '价格'], ['commission', '佣金'], ['slippage_cost', '滑点成本']] },
  { name: 'signals', label: '策略信号', columns: [['date', '日期'], ['intent', '意图'], ['weights', '目标权重'], ['reasons', '原因'], ['diagnostics', '诊断']] },
  { name: 'risk_events', label: '风控事件', columns: [['date', '日期'], ['code', '代码'], ['message', '说明'], ['details', '详情']] },
  { name: 'account_events', label: '现金事件', columns: [['date', '日期'], ['event_type', '事件'], ['symbol', '代码'], ['amount', '金额'], ['details', '详情']] },
  { name: 'matched_trades', label: '已平仓 FIFO 片段', columns: [['symbol', '代码'], ['entry_date', '建仓日'], ['exit_date', '平仓日'], ['quantity', '数量'], ['cost_basis', '成本'], ['proceeds', '收入'], ['realized_pnl', '已实现盈亏'], ['holding_sessions', '持有交易日']] },
  { name: 'open_lots', label: '未平仓批次', columns: [['symbol', '代码'], ['entry_date', '建仓日'], ['quantity', '数量'], ['cost_basis', '成本'], ['holding_sessions', '持有交易日']] }
]

const enumLabels: Record<string, Record<string, string>> = {
  side: { buy: '买入', sell: '卖出' },
  status: { pending: '待成交', filled: '已成交', partially_filled: '部分成交', expired: '已到期', cancelled: '已取消', unexecuted: '未执行', rejected: '已拒绝' },
  source: { strategy: '策略', risk: '风控' },
  intent: { hold: '保持', target: '目标组合' },
}

export function cell(value: unknown, field = ''): string {
  if (value == null) return '不可用'
  if (typeof value === 'string' && enumLabels[field]?.[value]) return enumLabels[field][value]
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

type DetailProps = { runId: string; defaultDate: string; startDate?: string; endDate?: string; refreshToken?: number; live?: boolean }
type PageState = { key: string; page: TablePage | null; loading: boolean; error?: string }

export function useTablePage(runId: string, name: TableName, filters: Omit<TableQuery, 'cursor' | 'limit'>, refreshToken = 0, live = false) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined])
  const [index, setIndex] = useState(0)
  const [retryToken, setRetryToken] = useState(0)
  const [liveToken, setLiveToken] = useState(0)
  const cursor = cursors[index]
  const dateFrom = filters.date_from
  const dateTo = filters.date_to
  const symbol = filters.symbol
  const contextDate = filters.context_date
  const key = JSON.stringify([runId, name, dateFrom, dateTo, symbol, contextDate, cursor])
  const [state, setState] = useState<PageState>({ key, page: null, loading: true })

  useEffect(() => {
    if (!live) return
    const timer = window.setInterval(() => setLiveToken(value => value + 1), 5000)
    return () => clearInterval(timer)
  }, [live])

  useEffect(() => {
    const controller = new AbortController()
    setState(old => ({ key, page: old.key === key ? old.page : null, loading: true }))
    // StrictMode cleans up its first effect before this microtask runs.
    void Promise.resolve().then(async () => {
      if (controller.signal.aborted) return
      try {
        const page = await api.runTable(runId, name, {
          limit: 100,
          ...(cursor ? { cursor } : {}),
          ...(dateFrom ? { date_from: dateFrom } : {}),
          ...(dateTo ? { date_to: dateTo } : {}),
          ...(symbol ? { symbol } : {}),
          ...(contextDate ? { context_date: contextDate } : {}),
        }, controller.signal)
        if (!controller.signal.aborted) setState({ key, page, loading: false })
      } catch (error) {
        if (!controller.signal.aborted) setState(old => ({ key, page: old.key === key ? old.page : null, loading: false, error: describeError(error) }))
      }
    })
    return () => controller.abort()
  }, [runId, name, dateFrom, dateTo, symbol, contextDate, cursor, key, refreshToken, retryToken, liveToken, live])

  const current = state.key === key ? state : { key, page: null, loading: true }
  const next = () => {
    if (!current.page?.next_cursor || current.loading) return
    setCursors(old => [...old.slice(0, index + 1), current.page!.next_cursor!])
    setIndex(index + 1)
  }
  const previous = () => { if (index > 0 && !current.loading) setIndex(index - 1) }
  return { ...current, index, next, previous, retry: () => setRetryToken(value => value + 1) }
}

const dateDescription: Partial<Record<TableName, string>> = {
  orders: '按信号日筛选', fills: '按成交日筛选', matched_trades: '按平仓日筛选', open_lots: '按建仓日筛选'
}
const globalTables: TableName[] = ['equity', 'signals', 'risk_events']

function TableView({ runId, name, dateFrom, dateTo, symbol, refreshToken, live }: { runId: string; name: TableName; dateFrom: string; dateTo: string; symbol: string; refreshToken?: number; live?: boolean }) {
  const definition = definitions.find(item => item.name === name) || definitions[0]
  const isGlobal = globalTables.includes(name)
  const data = useTablePage(runId, name, { date_from: dateFrom, date_to: dateTo, symbol: isGlobal ? undefined : symbol }, refreshToken, live)
  const rows = data.page?.items || []
  const start = data.index * 100 + (rows.length ? 1 : 0)
  const end = data.index * 100 + rows.length
  return <>
    <p className="muted">{dateDescription[name] || '按日期筛选'}；每页最多 100 行。{isGlobal && symbol && `${definition.label}是全局记录，不按标的筛选。`}</p>
    <ResultLoadNotice state={data} label={definition.label} onRetry={data.retry} />
    {rows.length > 0 && <div className="table-wrap"><table><thead><tr>{definition.columns.map(([field, label]) => <th key={field}>{label}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{definition.columns.map(([field]) => <td key={field} title={enumLabels[field] ? cell(row[field]) : undefined}>{cell(row[field], field)}</td>)}</tr>)}</tbody></table></div>}
    {!data.loading && !data.error && data.page && rows.length === 0 && <p className="empty">无{definition.label}记录。</p>}
    {data.page && <div className="inline-actions"><button type="button" disabled={data.loading || data.index === 0} onClick={data.previous}>上一页</button><span>第 {start}–{end} 行 / 共 {data.page.total} 行</span><button type="button" disabled={data.loading || !data.page.next_cursor} onClick={data.next}>下一页</button></div>}
  </>
}

export function ResultTables({ runId, defaultDate, startDate, endDate, refreshToken, live }: DetailProps) {
  const [selected, setSelected] = useState<TableName>('equity')
  const [custom, setCustom] = useState(false)
  const [dateFrom, setDateFrom] = useState(defaultDate)
  const [dateTo, setDateTo] = useState(defaultDate)
  const [draftFrom, setDraftFrom] = useState(defaultDate)
  const [draftTo, setDraftTo] = useState(defaultDate)
  const [symbol, setSymbol] = useState('')
  const [draftSymbol, setDraftSymbol] = useState('')
  useEffect(() => { if (!custom) { setDateFrom(defaultDate); setDateTo(defaultDate); setDraftFrom(defaultDate); setDraftTo(defaultDate) } }, [custom, defaultDate])
  const apply = (from: string, to: string, code: string) => {
    setCustom(true)
    setDateFrom(from)
    setDateTo(to)
    setSymbol(code.trim())
    setDraftFrom(from)
    setDraftTo(to)
    setDraftSymbol(code)
  }
  const recentMonth = () => {
    if (!defaultDate) return
    const date = new Date(`${defaultDate}T00:00:00Z`)
    date.setUTCDate(date.getUTCDate() - 29)
    apply(date.toISOString().slice(0, 10), defaultDate, draftSymbol)
  }
  return <section className="panel"><h2>账户与交易明细</h2><p className="muted">数据按服务端原始字段显示；FIFO 匹配行是片段，完整往返次数以指标为准。市场日期按 Asia/Shanghai 解释。</p>
    <form className="form-grid" onSubmit={event => { event.preventDefault(); apply(draftFrom, draftTo, draftSymbol) }}><label>开始日期<input type="date" value={draftFrom} onChange={event => setDraftFrom(event.target.value)} /></label><label>结束日期<input type="date" value={draftTo} onChange={event => setDraftTo(event.target.value)} /></label><label>标的代码<input type="text" value={draftSymbol} onChange={event => setDraftSymbol(event.target.value)} placeholder="可选" /></label><button type="submit">查询明细</button></form>
    <div className="inline-actions"><button type="button" disabled={!defaultDate} onClick={() => apply(defaultDate, defaultDate, draftSymbol)}>最近交易日</button><button type="button" disabled={!defaultDate} onClick={recentMonth}>最近30天</button>{startDate && endDate && <button type="button" onClick={() => apply(startDate, endDate, draftSymbol)}>全区间</button>}</div>
    <div className="result-tabs" role="tablist" aria-label="结果明细">{definitions.map(item => <button type="button" role="tab" aria-selected={selected === item.name} key={item.name} onClick={() => setSelected(item.name)}>{item.label}</button>)}</div>
    <TableView key={JSON.stringify([runId, selected, dateFrom, dateTo, symbol])} runId={runId} name={selected} dateFrom={dateFrom} dateTo={dateTo} symbol={symbol} refreshToken={refreshToken} live={live} />
  </section>
}
