import { useEffect, useRef, useState } from 'react'
import type { TableRow } from '../types'
import { metricValue } from './metricPresentation'
import { cell } from './ResultTables'

type ChartKind = 'equity' | 'drawdown' | 'monthly' | 'annual' | 'allocation'
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
        chart.setOption({ animation: false, tooltip: { position: 'top', formatter: (item: { value?: unknown[] }) => metricValue('return', item.value?.[2]) }, grid: { left: 55, right: 65, top: 20, bottom: 35 }, xAxis: { type: 'category', data: Array.from({ length: 12 }, (_, index) => `${index + 1}月`) }, yAxis: { type: 'category', data: years }, visualMap: { min: finite.length ? Math.min(...finite) : 0, max: finite.length ? Math.max(...finite) : 0, orient: 'vertical', right: 0, top: 'center', calculable: true }, series: [{ type: 'heatmap', data: values, label: { show: true, formatter: (item: { value?: unknown[] }) => item.value?.[2] == null ? '' : metricValue('return', item.value[2]) } }] })
      } else if (kind === 'allocation') {
        const dates = [...new Set(rows.map(row => String(row.date)))].sort()
        const symbols = [...new Set(rows.map(row => String(row.symbol)))].sort()
        chart.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 }, grid: { left: 55, right: 22, top: 40, bottom: 55 }, xAxis: { type: 'category', data: dates, axisLabel: { hideOverlap: true, rotate: 30 } }, yAxis: { type: 'value', axisLabel: { formatter: (value: number) => metricValue('weight', value) } }, dataZoom: [{ type: 'inside' }], series: symbols.map(symbol => { const weights = new Map(rows.filter(row => String(row.symbol) === symbol).map(row => [String(row.date), numeric(row.weight)])); return { name: symbol, type: 'bar', stack: 'holdings', data: dates.map(date => weights.get(date) ?? 0) } }) })
      } else {
        const series = kind === 'equity' ? [{ name: '策略净值', field: 'equity' }, { name: '基准净值', field: 'benchmark_equity' }] : kind === 'drawdown' ? [{ name: '回撤', field: 'drawdown' }] : [{ name: '收益', field: 'return' }]
        chart.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { show: series.length > 1, top: 0 }, grid: { left: 55, right: 22, top: 38, bottom: 55 }, xAxis: { type: 'category', data: data.map(row => String(row[axis] || '')), axisLabel: { hideOverlap: true, rotate: 30 } }, yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => metricValue(kind === 'equity' ? 'equity' : 'return', value) } }, dataZoom: [{ type: 'inside' }], series: series.map(item => ({ name: item.name, type: kind === 'annual' ? 'bar' : 'line', showSymbol: false, data: data.map(row => numeric(row[item.field])) })) })
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

export function ResultCharts({ curves, monthly, annual, positions }: { curves: TableRow[]; monthly: TableRow[]; annual: TableRow[]; positions: TableRow[] }) {
  const [view, setView] = useState<ChartKind>('equity')
  const definitions: { key: ChartKind; label: string; rows: TableRow[]; fields: [string, string][] }[] = [
    { key: 'equity', label: '权益与基准', rows: curves, fields: [['date', '日期'], ['equity', '账户权益金额'], ['benchmark_equity', '基准权益金额']] },
    { key: 'drawdown', label: '回撤', rows: curves, fields: [['date', '日期'], ['drawdown', '回撤']] },
    { key: 'monthly', label: '月度收益', rows: monthly, fields: [['period', '月份'], ['return', '收益']] },
    { key: 'annual', label: '年度收益', rows: annual, fields: [['period', '年份'], ['return', '收益']] },
    { key: 'allocation', label: '持仓权重', rows: positions, fields: [['date', '日期'], ['symbol', '代码'], ['weight', '权重']] }
  ]
  const selected = definitions.find(item => item.key === view) || definitions[0]
  return <section className="panel"><h2>绩效图表</h2><p className="muted">账户权益和基准权益均为金额；基准由服务端按本次初始现金归一。市场日期按 Asia/Shanghai 解释。图表仅为视觉展示，长序列可降采样；下方表格和服务端指标保持完整。</p><div className="result-tabs" role="tablist" aria-label="图表选择">{definitions.map(item => <button type="button" role="tab" aria-selected={view === item.key} key={item.key} onClick={() => setView(item.key)}>{item.label}</button>)}</div>
    {selected.rows.length ? <><Chart kind={selected.key} rows={selected.rows} /><details><summary>查看完整图表数据</summary><div className="table-wrap"><table><thead><tr>{selected.fields.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead><tbody>{selected.rows.map((row, index) => <tr key={index}>{selected.fields.map(([key]) => <td key={key}>{cell(row[key])}</td>)}</tr>)}</tbody></table></div></details></> : <p className="empty">暂无{selected.label}数据。</p>}</section>
}

export function ComparisonCharts({ curves }: { curves: Record<string, TableRow[]> }) {
  const [field, setField] = useState<'equity' | 'drawdown'>('equity')
  const host = useRef<HTMLDivElement>(null)
  const entries = Object.entries(curves)
  useEffect(() => {
    if (!host.current || !entries.length) return
    let disposed = false
    let chart: import('echarts/core').ECharts | undefined
    let visibility: IntersectionObserver | undefined
    const initialize = () => { void import('./chartEngine').then(({ createChart }) => {
      if (disposed || !host.current) return
      const dates = [...new Set(entries.flatMap(([, rows]) => rows.map(row => String(row.date))))].sort()
      const sampledDates = sample(dates.map(date => ({ date }))).map(row => String(row.date))
      chart = createChart(host.current)
      chart.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 }, grid: { left: 55, right: 22, top: 38, bottom: 55 }, xAxis: { type: 'category', data: sampledDates, axisLabel: { hideOverlap: true, rotate: 30 } }, yAxis: { type: 'value', scale: true, axisLabel: { formatter: (value: number) => metricValue(field, value) } }, dataZoom: [{ type: 'inside' }], series: entries.map(([id, rows]) => { const values = new Map(rows.map(row => [String(row.date), numeric(row[field])])); return { name: id, type: 'line', showSymbol: false, data: sampledDates.map(date => values.get(date) ?? null) } }) })
    }) }
    if (typeof IntersectionObserver !== 'undefined') { visibility = new IntersectionObserver(items => { if (items.some(item => item.isIntersecting)) { visibility?.disconnect(); initialize() } }); visibility.observe(host.current) } else initialize()
    return () => { disposed = true; visibility?.disconnect(); chart?.dispose() }
  }, [curves, field])
  return <section className="panel"><h2>可比运行曲线</h2><p className="muted">叠加的是服务端账户权益金额，不重新归一化；初始现金差异见冻结输入表。按完整服务端日期并列，不截取重算。图表长序列仅视觉降采样；完整原始曲线见下表。</p><div className="result-tabs"><button type="button" aria-pressed={field === 'equity'} onClick={() => setField('equity')}>叠加权益金额</button><button type="button" aria-pressed={field === 'drawdown'} onClick={() => setField('drawdown')}>叠加回撤</button></div><div className="result-chart" ref={host} role="img" aria-label="可比运行叠加曲线" /><details open><summary>完整曲线数据</summary><div className="table-wrap"><table><thead><tr><th>运行</th><th>日期</th><th>权益金额</th><th>回撤</th></tr></thead><tbody>{entries.flatMap(([id, rows]) => rows.map((row, index) => <tr key={`${id}-${index}`}><td>{id}</td><td>{cell(row.date)}</td><td>{cell(row.equity)}</td><td>{cell(row.drawdown)}</td></tr>))}</tbody></table></div></details></section>
}
