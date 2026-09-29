const rates = new Set(['total_return', 'annual_return', 'volatility', 'max_drawdown', 'benchmark_return', 'average_invested_ratio', 'return', 'drawdown', 'weight'])
const amounts = new Set(['final_equity', 'equity', 'benchmark_equity', 'realized_pnl', 'unrealized_pnl', 'commission_paid', 'slippage_cost', 'execution_cost', 'dividend_income', 'interest_income'])
export function metricValue(key: string, value: unknown, currency: unknown = ''): string {
  if (value == null) return '不可用'
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value)
  if (rates.has(key)) return `${(value * 100).toFixed(2)}%`
  if (key === 'relative_return_difference') return `${(value * 100).toFixed(2)} 个百分点`
  if (amounts.has(key)) return `${currency || '金额'} ${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
  if (key.endsWith('_count')) return value.toLocaleString('en-US')
  return value.toFixed(2)
}
export function DataDisclosure({ provenance }: { provenance?: Record<string, unknown> }) {
  if (!provenance) return null
  return <p className={provenance.is_demo === true ? 'callout warn' : 'muted'}>{provenance.is_demo === true ? '演示数据：合成绩效，不代表真实市场表现' : provenance.is_demo === false ? '非演示数据' : '演示标记未知'} · 来源 {String(provenance.source || '未知')}</p>
}
