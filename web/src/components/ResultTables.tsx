import { useState } from 'react'
import type { TableName, TableRow } from '../types'

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

export function ResultTables({ tables }: { tables: Partial<Record<TableName, TableRow[]>> }) {
  const [selected, setSelected] = useState<TableName>('equity')
  const definition = definitions.find(item => item.name === selected) || definitions[0]
  const rows = tables[selected] || []
  return <section className="panel"><h2>账户与交易明细</h2><p className="muted">数据按服务端原始字段显示；FIFO 匹配行是片段，完整往返次数以指标为准。市场日期按 Asia/Shanghai 解释。</p>
    <div className="result-tabs" role="tablist" aria-label="结果明细">{definitions.map(item => <button type="button" role="tab" aria-selected={selected === item.name} key={item.name} onClick={() => setSelected(item.name)}>{item.label}</button>)}</div>
    {rows.length ? <div className="table-wrap"><table><thead><tr>{definition.columns.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{definition.columns.map(([key]) => <td key={key} title={enumLabels[key] ? cell(row[key]) : undefined}>{cell(row[key], key)}</td>)}</tr>)}</tbody></table></div> : <p className="empty">无{definition.label}记录。</p>}
  </section>
}
