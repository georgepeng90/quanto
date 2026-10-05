import { useEffect, useState } from 'react'
import { api, describeError } from '../api'
import type { LearningArtifact, LearningChart, LearningTablePage } from '../learningTypes'
import { taskTime } from './timePresentation'

export const learningMetricLabels: Record<string, string> = {
  cash_interest: '现金利息', commissions: '佣金总额', final_cash: '期末现金', initial_cash: '初始资金', slippage_cost: '滑点成本', trading_days: '交易日数', turnover: '换手倍数', 'samples.train': '训练样本数', 'samples.validation': '验证样本数', 'samples.test': '测试样本数', final_equity: '最终资产', total_return: '总收益率', annualized_return: '年化收益（日末）', sharpe: '夏普比率（日末）', max_drawdown: '最大回撤（日末）', minute_max_drawdown: '盘中最大回撤', test_loss: '测试损失', validation_loss: '验证损失', best_validation_loss: '最佳验证损失', 'test.mse': '测试均方误差', 'test.mae': '测试绝对误差', 'test.samples': '测试样本数', 'train.mse': '训练均方误差', 'train.mae': '训练绝对误差', 'train.samples': '训练样本数', 'validation.mse': '验证均方误差', 'validation.mae': '验证绝对误差', 'validation.samples': '验证样本数',
  annual_return: '年化收益率', average_holding_sessions: '平均持有交易日', average_invested_ratio: '平均持仓比例',
  benchmark_return: '基准收益率', calmar: '卡玛比率', commission_paid: '已付佣金', decision_steps: '决策次数',
  dividend_income: '现金分红收入', execution_cost: '执行成本', fill_count: '成交笔数', interest_income: '现金利息收入',
  net_log_return: '累计净对数收益率', observed_sessions: '观测交易日数', open_lot_count: '期末未平仓批次数',
  realized_pnl: '已实现盈亏', relative_return_difference: '相对基准收益差', round_trip_count: '完整买卖轮次数',
  unrealized_pnl: '期末未实现盈亏', volatility: '年化波动率',
}
export function metricText(key: string, value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  if (key === 'relative_return_difference') return `${(value * 100).toFixed(2)} 个百分点`
  if (/return|drawdown/.test(key) || ['volatility', 'average_invested_ratio'].includes(key)) return `${(value * 100).toFixed(2)}%`
  if (value !== 0 && Math.abs(value) < .0001) return value.toExponential(4)
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })
}
export function record(value: unknown): Record<string, unknown> { return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {} }
export function flatMetrics(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).flatMap(([key, value]) => typeof value === 'object' && value !== null ? Object.entries(record(value)).map(([name, metric]) => [`${key}.${name}`, metric]) : [[key, value]]))
}
export const artifactName = (artifact: LearningArtifact) => String(artifact.manifest.name || record(artifact.manifest.config).name || artifact.id)
export function ResearchReplayLabel({ artifact }: { artifact: LearningArtifact }) {
  return record(artifact.manifest.metadata).allow_in_sample === true ? <span className="badge warn">样本内研究 · 非独立留出证据</span> : null
}
export function Metrics({ values }: { values: Record<string, unknown> }) {
  return <dl className="metric-grid">{Object.entries(flatMetrics(values)).map(([key, value]) => <div className="metric" key={key}><dt>{learningMetricLabels[key] || key}</dt><dd>{metricText(key, value)}</dd></div>)}</dl>
}
export function JsonDetails({ title, value }: { title: string; value: unknown }) {
  return <details className="run-source"><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>
}
function tableText(column: string, value: unknown) {
  if (['timestamp', 'signal_time', 'bar_start', 'bar_end'].includes(column) && typeof value === 'string') return taskTime(value, value || '—')
  return typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value ?? '—')
}
function Curve({ chart }: { chart: LearningChart }) {
  const points = chart.points.filter(point => Number.isFinite(point.equity))
  if (!points.length) return <p className="empty">暂无曲线数据。</p>
  const values = points.map(point => point.equity)
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1
  const path = points.map((point, index) => `${index ? 'L' : 'M'}${30 + index / Math.max(points.length - 1, 1) * 940},${190 - (point.equity - min) / range * 160}`).join(' ')
  return <><svg className="learning-curve" viewBox="0 0 1000 220" role="img" aria-label="分钟净值曲线"><title>分钟净值曲线</title><path d="M30 20 V190 H970" fill="none" stroke="#bccbd0" /><path d={path} fill="none" stroke="#087688" strokeWidth="2" /><text x="30" y="212">{taskTime(points[0].time)}</text><text x="970" y="212" textAnchor="end">{taskTime(points[points.length - 1].time)}</text></svg><p className="muted">显示 {points.length} / {chart.total_count} 个分钟点（最多 1000 点）；净值范围 {min.toFixed(2)}–{max.toFixed(2)}。指标按全量结果计算。</p></>
}

export function LearningResults({ id }: { id: string }) {
  const [artifact, setArtifact] = useState<LearningArtifact | null>(null)
  const [chart, setChart] = useState<LearningChart | null>(null)
  const [table, setTable] = useState<LearningTablePage | null>(null)
  const [tableName, setTableName] = useState('equity')
  const [cursors, setCursors] = useState<(number | string)[]>([0])
  const [error, setError] = useState('')
  const [tableError, setTableError] = useState('')
  const [loading, setLoading] = useState(true)
  const [downloading, setDownloading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    setArtifact(null); setChart(null); setError(''); setLoading(true)
    Promise.allSettled([api.learningArtifact('backtests', id, controller.signal), api.learningChart(id, controller.signal)]).then(([detail, curve]) => {
      if (controller.signal.aborted) return
      if (detail.status === 'fulfilled') setArtifact(detail.value)
      if (curve.status === 'fulfilled') setChart(curve.value)
      setError([detail, curve].filter(item => item.status === 'rejected').map(item => describeError((item as PromiseRejectedResult).reason)).join('；'))
      setLoading(false)
    })
    return () => controller.abort()
  }, [id])
  useEffect(() => {
    const controller = new AbortController()
    setTable(null); setTableError('')
    api.learningTable(id, tableName, cursors[cursors.length - 1], controller.signal).then(setTable).catch(value => { if (!controller.signal.aborted) setTableError(describeError(value)) })
    return () => controller.abort()
  }, [id, tableName, cursors])
  const summary = record(artifact?.manifest)
  const columns = table?.items.length ? Array.from(new Set(table.items.flatMap(row => Object.keys(row)))) : []
  const download = async () => {
    setDownloading(true); setError('')
    try {
      const blob = await api.exportLearning(id)
      const url = URL.createObjectURL(blob), anchor = document.createElement('a')
      anchor.href = url; anchor.download = `learning-${id}.zip`; anchor.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (value) { setError(describeError(value)) } finally { setDownloading(false) }
  }
  return <section className="panel" aria-label="回测结果"><div className="section-heading"><h2>回测结果 · {id}</h2><button onClick={download} disabled={downloading}>{downloading ? '导出中…' : '下载完整 ZIP'}</button></div>
    {artifact && <ResearchReplayLabel artifact={artifact} />}
    {error && <p role="alert" className="error">{error}</p>}{loading && <p>加载结果…</p>}
    {artifact && <><Metrics values={record(summary.metrics)} /><p className="muted">年化收益、Sharpe 与日末最大回撤使用交易日日末净值；盘中最大回撤使用分钟净值。时间显示为上海时间。</p><h3>执行假设与来源限制</h3>{<dl className="learning-assumptions">{Object.entries(record(summary.assumptions)).map(([key, value]) => <div key={key}><dt>{({ execution: '成交时钟', limitations: '来源限制', ledger: '账本类型', participation: '参与率', settlement: '结算', cash_interest: '现金利息', performance: '指标口径', default_lot_size: '整手假设', default_settlement_days: '结算交易日假设', drawdown_pause: '回撤暂停', tradability: '可交易性', corporate_actions: '公司行动' } as Record<string, string>)[key] || key}</dt><dd>{Array.isArray(value) ? value.join('；') : typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl>}<JsonDetails title="来源、限制与完整结果配置" value={artifact.manifest} /></>}
    {chart && <Curve chart={chart} />}
    <label>结果明细<select value={tableName} onChange={event => { setTableName(event.target.value); setCursors([0]) }}>{['equity', 'daily_equity', 'orders', 'fills', 'signals', 'positions'].map(name => <option key={name} value={name}>{({ equity: '分钟净值', daily_equity: '日末净值', orders: '订单', fills: '成交', signals: '信号', positions: '持仓' } as Record<string, string>)[name]}</option>)}</select></label>
    {tableError && <p role="alert" className="error">{tableError}</p>}{!table && !tableError && <p>加载明细…</p>}
    {table && <><p className="muted">明细时间已转换为上海时间；下载 ZIP 保留原始 UTC 时间。</p><div className="table-wrap"><table><thead><tr>{columns.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{table.items.map((row, index) => <tr key={index}>{columns.map(column => <td key={column}>{tableText(column, row[column])}</td>)}</tr>)}</tbody></table></div>{!table.items.length && <p className="empty">暂无明细。</p>}<div className="inline-actions"><button disabled={cursors.length <= 1} onClick={() => setCursors(previous => previous.slice(0, -1))}>上一页明细</button><span>第 {cursors.length} 页 · 每页最多 100 行</span><button disabled={table.next_cursor === null} onClick={() => { if (table.next_cursor !== null) setCursors(previous => [...previous, table.next_cursor!]) }}>下一页明细</button></div></>}
  </section>
}
