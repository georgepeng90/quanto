import { useState } from 'react'
import { Link } from 'react-router-dom'
import { JsonDetails, metricText } from './LearningResults'
import { stateLabel } from './RunProgress'
import { taskTime } from './timePresentation'
import type { BacktestCatalogRow, RunState } from '../types'

type Sort = 'date' | 'return' | 'cagr' | 'drawdown' | 'sharpe'
const kinds = { fixed: '固定策略', policy: 'PPO 策略', model: '分钟模型' }
const conditionLabels: Record<string, string> = { period: '回测区间', symbols: 'ETF 范围', dataset_content_hash: '数据版本', price_mode: '价格口径', execution: '成交与成本', risk: '风险约束', initial_cash: '初始资金', metrics: '指标定义', valuation_frequency: '估值频率' }
const metricLabels: Record<string, string> = { total_return: '累计收益率', annual_return: '年化收益率', annualized_return: '年化收益率', max_drawdown: '最大回撤', sharpe: 'Sharpe' }
const value = (row: BacktestCatalogRow, sort: Sort): number | null => {
  if (sort === 'date') { const date = row.created_at ? new Date(row.created_at).getTime() : null; return date != null && Number.isFinite(date) ? date : null }
  const metric = sort === 'return' ? row.metrics?.total_return : sort === 'cagr' ? row.metrics?.annual_return ?? row.metrics?.annualized_return : row.metrics?.[sort === 'drawdown' ? 'max_drawdown' : 'sharpe']
  return typeof metric === 'number' && Number.isFinite(metric) ? metric : null
}
const showMetric = (row: BacktestCatalogRow, key: string) => metricText(key, row.state === 'succeeded' ? key === 'annual_return' ? row.metrics?.annual_return ?? row.metrics?.annualized_return : row.metrics?.[key] : null)
const display = (item: unknown) => item == null ? '—' : typeof item === 'string' ? item : JSON.stringify(item)
const basket = (symbols: string[]) => symbols.length ? `${symbols.length} 只 · ${symbols.slice(0, 3).join('、')}${symbols.length > 3 ? '…' : ''}` : '—'
const shortId = (id: string) => id.length > 12 ? `${id.slice(0, 8)}…` : id
const canonical = (item: unknown): string => JSON.stringify(item, (_, value) => value && typeof value === 'object' && !Array.isArray(value) ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value)

function Comparison({ rows }: { rows: BacktestCatalogRow[] }) {
  if (rows.length < 2) return null
  const keys = [...new Set(rows.flatMap(row => Object.keys(row.conditions || {})))]
  const differences = keys.filter(key => new Set(rows.map(row => canonical(row.conditions?.[key]))).size > 1)
  const comparable = rows.every(row => row.state === 'succeeded' && !!row.metrics && !row.error && !row.result_error && !!row.comparison_key && row.comparison_key === rows[0].comparison_key) && differences.length === 0
  const metricKeys = [...new Set(rows.flatMap(row => Object.keys(row.metrics || {}).map(key => key === 'annualized_return' ? 'annual_return' : key)))]
  return <section className="panel" aria-label="所选回测比较"><h2>所选回测比较</h2>
    <p role="status" className={comparable ? 'callout success' : 'callout warn'}>{comparable ? '条件一致，可直接比较这些结果。' : '条件不同或结果未完成，不可直接排名；先核对下方差异。'}</p>
    <div className="table-wrap"><table aria-label="所选回测指标比较"><thead><tr><th>指标</th>{rows.map(row => <th key={row.id}><Link to={row.detail_url}>{row.name}</Link><small> {row.id}</small></th>)}</tr></thead><tbody>{metricKeys.map(key => <tr key={key}><th>{metricLabels[key] || key}</th>{rows.map(row => <td key={row.id}>{showMetric(row, key)}</td>)}</tr>)}</tbody></table></div>
    {!metricKeys.length && <p className="empty">所选回测尚无可用指标。</p>}
    {differences.length > 0 && <><h3>条件差异</h3><div className="table-wrap"><table aria-label="回测条件差异"><thead><tr><th>条件</th>{rows.map(row => <th key={row.id}>{row.id}</th>)}</tr></thead><tbody>{differences.map(key => <tr key={key}><th>{conditionLabels[key] || key}<small> {key}</small></th>{rows.map(row => <td key={row.id}><code>{display(row.conditions?.[key])}</code></td>)}</tr>)}</tbody></table></div></>}
    {rows.map(row => <JsonDetails key={row.id} title={'完整条件 · ' + row.id} value={row.conditions} />)}
  </section>
}

export function BacktestCatalog({ rows, complete, busy, refresh }: { rows: BacktestCatalogRow[]; complete: boolean; busy: boolean; refresh: () => void }) {
  const [kindFilter, setKindFilter] = useState<BacktestCatalogRow['kind'] | 'all'>('all')
  const [stateFilter, setStateFilter] = useState<RunState | 'all'>('all')
  const [groupFilter, setGroupFilter] = useState('all')
  const [sort, setSort] = useState<Sort>('date')
  const [comparisonIds, setComparisonIds] = useState<string[]>([])
  const groups = [...new Set(rows.filter(row => row.group_id).map(row => row.group_id!))]
  const visible = complete ? rows.filter(row => (kindFilter === 'all' || row.kind === kindFilter) && (stateFilter === 'all' || row.state === stateFilter) && (groupFilter === 'all' || (row.group_id || 'single') === groupFilter)).sort((a, b) => {
    const left = value(a, sort), right = value(b, sort)
    if (left == null) return right == null ? a.id.localeCompare(b.id) : 1
    if (right == null) return -1
    return (sort === 'drawdown' ? left - right : right - left) || a.id.localeCompare(b.id)
  }) : []
  const selected = comparisonIds.flatMap(id => rows.find(row => row.id === id) || [])
  return <><section className="panel"><div className="section-heading"><div><h2>全部回测</h2><p>包含排队、运行和已结束任务。排序仅按指标；条件一致的结果才可直接比较。时间按北京时间（Asia/Shanghai，UTC+8）显示。</p></div><button type="button" disabled={busy} onClick={refresh}>刷新回测</button></div>
    <div className="form-grid"><label>类型筛选<select disabled={!complete} value={kindFilter} onChange={event => setKindFilter(event.target.value as typeof kindFilter)}><option value="all">全部类型</option>{Object.entries(kinds).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}</select></label><label>状态筛选<select disabled={!complete} value={stateFilter} onChange={event => setStateFilter(event.target.value as typeof stateFilter)}><option value="all">全部状态</option>{Object.entries(stateLabel).map(([state, label]) => <option key={state} value={state}>{label}</option>)}</select></label><label>批次筛选<select disabled={!complete} value={groupFilter} onChange={event => setGroupFilter(event.target.value)}><option value="all">全部批次</option><option value="single">非批次任务</option>{groups.map(group => <option key={group} value={group}>{group}</option>)}</select></label><label>指标排序<select disabled={!complete} value={sort} onChange={event => setSort(event.target.value as Sort)}><option value="date">提交时间（新到旧）</option><option value="return">累计收益（高到低）</option><option value="cagr">年化收益（高到低）</option><option value="drawdown">最大回撤（低到高）</option><option value="sharpe">Sharpe（高到低）</option></select></label></div>
    {!complete ? <p role="status" className="callout">{busy ? '正在加载全部回测…' : '回测目录未读取完整，请刷新重试。'}</p> : <><p className="muted">{rows.length} 条回测 · 当前显示 {visible.length} 条</p>{visible.length ? <div className="table-wrap catalog-table-wrap" role="region" aria-label="全部回测表格" tabIndex={0}><table aria-label="全部回测"><thead><tr><th>比较</th><th>回测</th><th>类型 / 方法</th><th>状态</th><th>区间 / 标的</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th><th>Sharpe</th><th>提交时间</th></tr></thead><tbody>{visible.map(row => <tr key={row.id}><td><label className="catalog-check"><input type="checkbox" aria-label={'比较 ' + row.id} checked={comparisonIds.includes(row.id)} onChange={() => setComparisonIds(old => old.includes(row.id) ? old.filter(id => id !== row.id) : [...old, row.id])} /></label></td><td><Link to={row.detail_url}>{row.name}</Link><small className="catalog-id" title={row.id}>{shortId(row.id)}</small></td><td>{kinds[row.kind]}<small className="catalog-id">{row.method}</small></td><td><span className="badge">{stateLabel[row.state] || row.state}</span>{(row.error || row.result_error) && <small className="catalog-error" title={row.error || row.result_error || ''}>{row.error || row.result_error}</small>}</td><td>{row.start || '—'} — {row.end || '—'}<small className="catalog-id" title={row.symbols.join('、')}>{basket(row.symbols)}</small></td><td>{showMetric(row, 'total_return')}</td><td>{metricText('annual_return', row.state === 'succeeded' ? row.metrics?.annual_return ?? row.metrics?.annualized_return : null)}</td><td>{showMetric(row, 'max_drawdown')}</td><td>{showMetric(row, 'sharpe')}</td><td>{taskTime(row.created_at, '—')}</td></tr>)}</tbody></table></div> : <p className="empty">暂无匹配回测。</p>}</>}
  </section><Comparison rows={selected} /></>
}
