import { useEffect, useRef, useState } from 'react'
import type { ChartData, ChartName, ChartQuery, TableLoadState, TableRow } from '../types'
import { api, describeError } from '../api'
import { ResultLoadNotice } from './ResultLoadNotice'
import { metricValue } from './metricPresentation'
import { cell } from './ResultTables'

type ChartKind = 'equity' | 'drawdown' | 'monthly' | 'annual' | 'allocation'
const returnColors = { positive: '#dc2626', negative: '#15803d', neutral: '#f3f4f6' }
const numeric = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : null
const sample = (rows: TableRow[]) => rows.length <= 1200 ? rows : rows.filter((_, index) => index % Math.ceil(rows.length / 1200) === 0 || index === rows.length - 1)

function Chart({ kind, rows }: { kind: ChartKind; rows: TableRow[] }) {
  const host = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!host.current || !rows.length) return
    let disposed = false
    let chart: import('echarts/core').ECharts | undefined
    let observer: ResizeObserver | undefined
    let visibility: IntersectionObserver | undefined
    const initialize = () => { void import('./chartEngine').then(({ createChart }) => {
      if (disposed || !host.current) return
      chart = createChart(host.current)
      const data = sample(rows)
      const axis = kind === 'monthly' || kind === 'annual' ? 'period' : 'date'
      if (kind === 'monthly') {
        const years = [...new Set(rows.map(row => String(row.period).slice(0, 4)))].sort()
        const values = rows.map(row => [Number(String(row.period).slice(5, 7)) - 1, years.indexOf(String(row.period).slice(0, 4)), numeric(row.return)])
        const finite = rows.map(row => numeric(row.return)).filter((value): value is number => value !== null)
        const limit = Math.max(0, ...finite.map(Math.abs)) || 0.01
        chart.setOption({ animation: false, tooltip: { position: 'top', formatter: (item: { value?: unknown[] }) => metricValue('return', item.value?.[2]) }, grid: { left: 55, right: 85, top: 20, bottom: 35 }, xAxis: { type: 'category', data: Array.from({ length: 12 }, (_, index) => `${index + 1}月`) }, yAxis: { type: 'category', data: years }, visualMap: { min: -limit, max: limit, dimension: 2, orient: 'vertical', right: 0, top: 'center', calculable: true, formatter: (value: number) => metricValue('return', value), inRange: { color: [returnColors.negative, returnColors.neutral, returnColors.positive] } }, series: [{ type: 'heatmap', data: values, label: { show: true, formatter: (item: { value?: unknown[] }) => item.value?.[2] == null ? '' : metricValue('return', item.value[2]) } }] })
      } else if (kind === 'allocation') {
        const dates = [...new Set(rows.map(row => String(row.date)))].sort()
        const symbols = [...new Set(rows.map(row => String(row.symbol)))].sort()
        chart.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 }, grid: { left: 55, right: 22, top: 40, bottom: 55 }, xAxis: { type: 'category', data: dates, axisLabel: { hideOverlap: true, rotate: 30 } }, yAxis: { type: 'value', axisLabel: { formatter: (value: number) => metricValue('weight', value) } }, dataZoom: [{ type: 'inside' }], series: symbols.map(symbol => { const weights = new Map(rows.filter(row => String(row.symbol) === symbol).map(row => [String(row.date), numeric(row.weight)])); return { name: symbol, type: 'bar', stack: 'holdings', data: dates.map(date => weights.get(date) ?? 0) } }) })
      } else {
        const series = kind === 'equity' ? [{ name: '策略净值', field: 'equity' }, { name: '基准净值', field: 'benchmark_equity' }] : kind === 'drawdown' ? [{ name: '回撤', field: 'drawdown' }] : [{ name: '收益', field: 'return' }]
        chart.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { show: series.length > 1, top: 0 }, grid: { left: 55, right: 22, top: 38, bottom: 55 }, xAxis: { type: 'category', data: data.map(row => String(row[axis] || '')), axisLabel: { hideOverlap: true, rotate: 30 } }, yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => metricValue(kind === 'equity' ? 'equity' : 'return', value) } }, dataZoom: [{ type: 'inside' }], series: series.map(item => ({ name: item.name, type: kind === 'annual' ? 'bar' : 'line', showSymbol: false, data: data.map(row => { const value = numeric(row[item.field]); return kind === 'annual' && value !== null ? { value, itemStyle: { color: value > 0 ? returnColors.positive : value < 0 ? returnColors.negative : returnColors.neutral } } : value }) })) })
      }
      if (typeof ResizeObserver !== 'undefined') { observer = new ResizeObserver(() => chart?.resize()); observer.observe(host.current) }
    }) }
    if (typeof IntersectionObserver !== 'undefined') {
      visibility = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) { visibility?.disconnect(); initialize() } })
      visibility.observe(host.current)
    } else initialize()
    return () => { disposed = true; visibility?.disconnect(); observer?.disconnect(); chart?.dispose() }
  }, [kind, rows])
  return <div ref={host} className="result-chart" role="img" aria-label={`${kind}图表，日期和数值详见下方数据表`} />
}

export function ResultCharts({ runId, startDate, endDate, refreshToken = 0 }: { runId: string; startDate?: string; endDate?: string; refreshToken?: number }) {
  const [view, setView] = useState<ChartKind>('equity')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [query, setQuery] = useState<ChartQuery>({ max_points: 1200 })
  const [data, setData] = useState<ChartData | null>(null)
  const [state, setState] = useState<TableLoadState>({ loading: true })
  const [retry, setRetry] = useState(0)
  const [expanded, setExpanded] = useState(false)
  const definitions: { key: ChartKind; name: ChartName; label: string; fields: [string, string][] }[] = [
    { key: 'equity', name: 'curves', label: '权益与基准', fields: [['date', '日期'], ['equity', '账户权益金额'], ['benchmark_equity', '基准权益金额']] },
    { key: 'drawdown', name: 'curves', label: '回撤', fields: [['date', '日期'], ['drawdown', '回撤']] },
    { key: 'monthly', name: 'monthly_returns', label: '月度收益', fields: [['period', '月份'], ['return', '收益']] },
    { key: 'annual', name: 'annual_returns', label: '年度收益', fields: [['period', '年份'], ['return', '收益']] },
    { key: 'allocation', name: 'allocation', label: '持仓权重', fields: [['date', '日期'], ['symbol', '代码'], ['weight', '权重']] }
  ]
  const selected = definitions.find(item => item.key === view) || definitions[0]
  useEffect(() => {
    const controller = new AbortController()
    setData(null); setState({ loading: true }); setExpanded(false)
    void api.runChart(runId, selected.name, query, controller.signal).then(value => {
      if (!controller.signal.aborted) { setData(value); setState({ loading: false }) }
    }).catch(value => { if (!controller.signal.aborted) setState({ loading: false, error: describeError(value) }) })
    return () => controller.abort()
  }, [runId, selected.name, query, refreshToken, retry])
  const rows = data?.items || []
  return <section className="panel"><h2>绩效图表</h2><p className="muted">账户权益和基准权益均为金额；基准由服务端按本次初始现金归一。市场日期按 Asia/Shanghai 解释。图表使用有界摘要，最终指标始终来自完整报告。</p>
    <form onSubmit={event => { event.preventDefault(); if (from && to && from > to) { setState({ loading: false, error: '起始日不能晚于结束日' }); return } setQuery({ date_from: from || undefined, date_to: to || undefined, max_points: 1200 }) }}><div className="form-grid"><label>图表起始日<input type="date" min={startDate} max={endDate} value={from} onChange={event => setFrom(event.target.value)} /></label><label>图表结束日<input type="date" min={startDate} max={endDate} value={to} onChange={event => setTo(event.target.value)} /></label></div><button type="submit">更新图表</button><span className="muted"> 留空查看整个回测区间。</span></form>
    <div className="result-tabs" role="tablist" aria-label="图表选择">{definitions.map(item => <button type="button" role="tab" aria-selected={view === item.key} key={item.key} onClick={() => setView(item.key)}>{item.label}</button>)}</div>
    <ResultLoadNotice state={state} label={selected.label} />{state.error && <button type="button" onClick={() => setRetry(value => value + 1)}>重试图表</button>}
    {data && <p className="muted">显示 {rows.length} 个点，原始记录 {data.total_rows} 行。{data.sampled ? '服务端已采样；完整数据请通过明细分页或导出查看。' : '当前区间未采样。'}</p>}
    {rows.length ? <><Chart kind={selected.key} rows={rows} /><details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}><summary>查看图表数据（最多 1200 点）</summary>{expanded && <div className="table-wrap"><table><thead><tr>{selected.fields.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{selected.fields.map(([key]) => <td key={key}>{cell(row[key])}</td>)}</tr>)}</tbody></table></div>}</details></> : !state.loading && !state.error ? <p className="empty">暂无{selected.label}数据。</p> : null}</section>
}

export function ComparisonCharts({ curves }: { curves: Record<string, TableRow[]> }) {
  const [field, setField] = useState<'equity' | 'drawdown'>('equity')
  const [expanded, setExpanded] = useState(false)
  const [tableRun, setTableRun] = useState(Object.keys(curves)[0] || '')
  const host = useRef<HTMLDivElement>(null)
  const entries = Object.entries(curves)
  useEffect(() => {
    if (!host.current || !entries.length) return
    let disposed = false
    let chart: import('echarts/core').ECharts | undefined
    let visibility: IntersectionObserver | undefined
    const initialize = () => { void import('./chartEngine').then(({ createChart }) => {
      if (disposed || !host.current) return
      chart = createChart(host.current)
      chart.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 }, grid: { left: 55, right: 22, top: 38, bottom: 55 }, xAxis: { type: 'time', axisLabel: { hideOverlap: true, rotate: 30, formatter: (value: number) => new Date(value).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' }) } }, yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => metricValue(field, value) } }, dataZoom: [{ type: 'inside' }], series: entries.map(([id, rows]) => ({ name: id, type: 'line', showSymbol: false, data: rows.map(row => [Date.parse(`${row.date}T00:00:00+08:00`), numeric(row[field])]) })) })
    }) }
    if (typeof IntersectionObserver !== 'undefined') { visibility = new IntersectionObserver(items => { if (items.some(item => item.isIntersecting)) { visibility?.disconnect(); initialize() } }); visibility.observe(host.current) } else initialize()
    return () => { disposed = true; visibility?.disconnect(); chart?.dispose() }
  }, [curves, field])
  const selectedRun = curves[tableRun] ? tableRun : entries[0]?.[0]
  return <section className="panel"><h2>可比运行曲线</h2><p className="muted">叠加服务端账户权益金额，不重新归一化；初始现金差异见冻结输入表。每条曲线最多 1200 个摘要点，按服务端日期并列；摘要不用于重算指标。完整曲线可从各运行导出。</p><div className="result-tabs"><button type="button" aria-pressed={field === 'equity'} onClick={() => setField('equity')}>叠加权益金额</button><button type="button" aria-pressed={field === 'drawdown'} onClick={() => setField('drawdown')}>叠加回撤</button></div><div className="result-chart" ref={host} role="img" aria-label="可比运行叠加曲线" /><details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}><summary>图表摘要数据</summary>{expanded && <><label>图表数据运行<select value={selectedRun} onChange={event => setTableRun(event.target.value)}>{entries.map(([id]) => <option key={id} value={id}>{id}</option>)}</select></label><div className="table-wrap"><table><thead><tr><th>日期</th><th>权益金额</th><th>回撤</th></tr></thead><tbody>{(curves[selectedRun] || []).map((row, index) => <tr key={index}><td>{cell(row.date)}</td><td>{cell(row.equity)}</td><td>{cell(row.drawdown)}</td></tr>)}</tbody></table></div></>}</details></section>
}
