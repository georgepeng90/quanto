import { Link } from 'react-router-dom'
import type { RunRecord, RunResult } from '../types'
import { DataDisclosure, metricValue } from './metricPresentation'
import { stateLabel } from './RunProgress'
import { taskTime } from './timePresentation'
import { FrameworkProvenance } from './FrameworkProvenance'

const metrics = [
  ['total_return', '累计收益'], ['annual_return', '年化收益'],
  ['max_drawdown', '最大回撤'], ['sharpe', '夏普比率'], ['final_equity', '最终权益'],
] as const

export function RunSummary({ run, provenance, result }: {
  run: RunRecord; provenance?: Record<string, unknown>; result?: RunResult
}) {
  const name = provenance?.strategy_name
  const symbols = provenance?.symbols
  return <article className="record run-summary" aria-label={`回测 ${run.run_id}`}>
    <div className="record-head">
      <strong><Link to={run.detail_url}>{typeof name === 'string' && name ? name : run.run_id}</Link></strong>
      <span className="badge">{stateLabel[run.state]}</span>
      {provenance && <span className={`badge${provenance.is_demo === true ? ' warn' : ''}`} title={provenance.is_demo === true ? '合成数据，不代表真实市场表现' : undefined}>
        {provenance.is_demo === true ? '演示数据 · 合成绩效' : provenance.is_demo === false ? '非演示数据' : '数据标记未知'}
      </span>}
      {typeof provenance?.price_mode === 'string' && <span className="badge">{provenance.price_mode === 'raw_cash' ? '原始现金口径' : '复权研究口径'}</span>}
    </div>
    <div className="run-context">
      <span>回测区间 {String(provenance?.start || '读取中')} — {String(provenance?.end || '读取中')}</span>
      <span>初始资金 {metricValue('initial_cash', provenance?.initial_cash, provenance?.currency)}</span>
      {run.state === 'succeeded' && Array.isArray(result?.held_symbols) && <span>实际持有 {result.held_symbols.length} 只</span>}
      {Array.isArray(symbols) && <span className="muted">回测范围 {symbols.length} 只</span>}
    </div>
    {run.state === 'succeeded' ? result?.metrics ? <dl className="run-metrics">
      {metrics.map(([key, label]) => {
        const value = result.metrics?.[key]
        const color = (key === 'total_return' || key === 'annual_return') && typeof value === 'number' && Number.isFinite(value)
          ? value > 0 ? 'return-positive' : value < 0 ? 'return-negative' : undefined : undefined
        return <div key={key}><dt>{label}</dt><dd className={color} title={String(value ?? '')}>{metricValue(key, value, provenance?.currency)}</dd></div>
      })}
    </dl> : <p className="muted">{result ? '绩效摘要不可用。' : '正在读取绩效摘要…'}</p>
      : <p className="muted">已完成至 {run.completed_through || '尚无完整交易日'} · 尚无最终绩效</p>}
    {run.error && <p className="error">{run.error}</p>}
    <div className="run-meta"><small>提交 {taskTime(run.created_at, '历史记录未知')}</small></div>
    <details className="run-source">
      <summary>参数与来源</summary>
      <p>运行 {run.run_id} · 实验 {run.experiment_id} · 修订 {run.experiment_revision}</p>
      <p>策略修订 {String(provenance?.strategy_revision_id || '读取中')} · 数据版本 {String(provenance?.dataset_version_id || '读取中')}</p>
      <p>有效参数 <code>{provenance?.effective_strategy_params ? JSON.stringify(provenance.effective_strategy_params) : '读取中'}</code></p>
      {provenance?.sample_purpose != null && <p>样本用途 {String(provenance.sample_purpose)}</p>}
      <DataDisclosure provenance={provenance} />
      <FrameworkProvenance value={provenance} />
      <p>已完成至 {run.completed_through || '尚无完整交易日'}</p>
      <small>开始 {taskTime(run.started_at)} · 结束 {taskTime(run.finished_at)}</small>
    </details>
  </article>
}
