import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { JsonDetails, Metrics, record } from '../components/LearningResults'
import { cell } from '../components/ResultTables'
import type { InvestingArtifact, InvestingChart, InvestingTablePage } from '../investingTypes'

const columnLabels: Record<string, string> = {
  date: '日期', cash: '现金余额', receivables: '应收款项', market_value: '持仓市值', equity: '账户权益', stale_value: '陈旧报价估值', drawdown: '回撤',
  id: '订单编号', order_id: '订单编号', symbol: '证券代码', side: '买卖方向', quantity: '数量', price: '成交价格',
  signal_date: '信号日期', execution_date: '执行日期', trade_date: '成交日期', sellable_date: '可卖日期',
  status: '订单状态', reason: '原因', source: '来源', filled_quantity: '已成交数量', expired_quantity: '到期未成交数量',
  lot_size: '每手数量', settlement_sessions: '结算交易日数', full_exit: '是否清仓', commission: '佣金', slippage_cost: '滑点成本',
  intent: '策略意图', weights: '目标权重', reasons: '决策原因', diagnostics: '诊断信息',
  approved_intent: '风控后意图', approved_weights: '风控后目标权重', approved_reasons: '风控后原因', approved_diagnostics: '风控后诊断信息',
  shares: '持仓份额', sellable_shares: '可卖份额', weight: '持仓权重', cost_basis: '持仓成本', unrealized_pnl: '未实现盈亏', realized_pnl: '已实现盈亏',
}

function DailyCurve({ chart }: { chart: InvestingChart }) {
  const points = chart.points.filter(point => Number.isFinite(point.equity))
  if (!points.length) return <p>暂无曲线数据。</p>
  const min = Math.min(...points.map(point => point.equity)), max = Math.max(...points.map(point => point.equity)), range = max - min || 1
  const path = points.map((point, index) => `${index ? 'L' : 'M'}${30 + index / Math.max(points.length - 1, 1) * 940},${190 - (point.equity - min) / range * 160}`).join(' ')
  return <><svg className="learning-curve" viewBox="0 0 1000 220" role="img" aria-label="每日净值曲线"><title>每日净值曲线</title><path d="M30 20 V190 H970" fill="none" stroke="#bccbd0" /><path d={path} fill="none" stroke="#087688" strokeWidth="2" /><text x="30" y="212">{points[0].time.slice(0, 10)}</text><text x="970" y="212" textAnchor="end">{points[points.length - 1].time.slice(0, 10)}</text></svg><p className="muted">显示 {points.length} / {chart.total_count} 个日线点（最多 1000 点）；指标使用完整每日结果。</p></>
}
function InvestingResults({ id }: { id: string }) {
  const [artifact, setArtifact] = useState<InvestingArtifact | null>(null), [chart, setChart] = useState<InvestingChart | null>(null)
  const [table, setTable] = useState<InvestingTablePage | null>(null), [tableName, setTableName] = useState('daily_equity')
  const [cursors, setCursors] = useState<(number | string)[]>([0]), [error, setError] = useState(''), [tableError, setTableError] = useState(''), [downloading, setDownloading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    Promise.allSettled([api.investingArtifact('backtests', id, controller.signal), api.investingChart(id, controller.signal)]).then(([detail, curve]) => {
      if (controller.signal.aborted) return
      if (detail.status === 'fulfilled') setArtifact(detail.value)
      if (curve.status === 'fulfilled') setChart(curve.value)
      setError([detail, curve].filter(value => value.status === 'rejected').map(value => describeError((value as PromiseRejectedResult).reason)).join('；'))
    })
    return () => controller.abort()
  }, [id])
  useEffect(() => {
    const controller = new AbortController(); setTable(null); setTableError('')
    api.investingTable(id, tableName, cursors[cursors.length - 1], controller.signal).then(value => { if (!controller.signal.aborted) setTable(value) }).catch(value => { if (!controller.signal.aborted) setTableError(describeError(value)) })
    return () => controller.abort()
  }, [id, tableName, cursors])
  const download = async () => {
    setDownloading(true); setError('')
    try { const blob = await api.exportInvesting(id), url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = `investing-${id}.zip`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }
    catch (value) { setError(describeError(value)) } finally { setDownloading(false) }
  }
  const columns = Array.from(new Set(table?.items.flatMap(row => Object.keys(row)) || []))
  return <section className="panel" aria-label="日线回测结果"><div className="section-heading"><h2>日线回测结果 · {id}</h2><button disabled={downloading} onClick={() => void download()}>{downloading ? '导出中…' : '下载完整 ZIP'}</button></div>
    {error && <p role="alert" className="error">{error}</p>}{artifact && <><Metrics values={record(artifact.manifest.metrics)} /><JsonDetails title="执行假设、数据来源与完整配置" value={artifact.manifest} /></>}{chart && <DailyCurve chart={chart} />}
    <label>结果明细<select value={tableName} onChange={event => { setTableName(event.target.value); setCursors([0]) }}>{Object.entries({ daily_equity: '每日净值', orders: '订单', fills: '成交', signals: '决策信号', positions: '持仓' }).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
    {tableError && <p role="alert" className="error">{tableError}</p>}{!table && !tableError && <p>加载明细…</p>}{table && <><div className="table-wrap"><table><thead><tr>{columns.map(key => <th key={key}>{columnLabels[key] || key}</th>)}</tr></thead><tbody>{table.items.map((row, index) => <tr key={index}>{columns.map(key => <td key={key}>{typeof row[key] === 'boolean' ? row[key] ? '是' : '否' : cell(row[key], key.replace(/^approved_/, ''))}</td>)}</tr>)}</tbody></table></div>{!table.items.length && <p>暂无明细。</p>}<div className="inline-actions"><button disabled={cursors.length < 2} onClick={() => setCursors(previous => previous.slice(0, -1))}>上一页明细</button><span>第 {cursors.length} 页 · 每页最多 100 行</span><button disabled={table.next_cursor === null} onClick={() => { if (table.next_cursor !== null) setCursors(previous => [...previous, table.next_cursor!]) }}>下一页明细</button></div></>}
  </section>
}

export function InvestingPage() {
  const [params] = useSearchParams()
  const id = params.get('backtest')
  return <div className="page-stack investing-page"><header className="page-heading"><h1>日线回测报告</h1><Link to="/runs">全部回测</Link></header>{id ? <InvestingResults key={id} id={id} /> : <p>请从回测目录选择历史报告。<Link to="/models">进入强化学习模型</Link></p>}</div>
}
