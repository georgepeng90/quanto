import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, describeError } from '../api'
import type { InvestingModelSummary, TrainingHistoryItem } from '../investingTypes'
import { metricText } from './LearningResults'

const metrics = [['total_return', '总收益率'], ['max_drawdown', '最大回撤（日末）'], ['sharpe', '夏普比率（日末）']] as const

export function CurrentModelSnapshot({ model }: { model: InvestingModelSummary }) {
  return <Snapshot key={`${model.id}:${model.latest_version_id}`} model={model} />
}

function Snapshot({ model }: { model: InvestingModelSummary }) {
  const [latest, setLatest] = useState<TrainingHistoryItem | null>(null)
  const [error, setError] = useState(''), [retry, setRetry] = useState(0)
  useEffect(() => {
    if (!model.latest_version_id) return
    const controller = new AbortController()
    setError('')
    api.investingModelHistory(model.id, { kind: 'versions', page: 1, page_size: 1, round_number: model.version_count }, controller.signal).then(page => {
      if (controller.signal.aborted) return
      const item = page.items.find(item => item.id === model.latest_version_id)
      if (!item) throw new Error('未找到当前版本，请刷新模型后重试。')
      setLatest(item)
    }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [model.id, model.latest_version_id, model.version_count, retry])

  return <section className="panel" aria-label="当前选定快照">
    <div className="section-heading"><h2>当前选定快照</h2>{latest?.selection_valid === true && latest.best_source_version_id && <Link to={`/runs?model=${encodeURIComponent(model.id)}&version=${encodeURIComponent(latest.best_source_version_id)}`}>使用当前快照回测</Link>}</div>
    {!model.latest_version_id && <p className="empty">尚未训练，暂无可用快照。</p>}
    {model.latest_version_id && !latest && !error && <p role="status">加载当前快照…</p>}
    {error && <p role="alert" className="error">{error} <button onClick={() => setRetry(value => value + 1)}>重试快照</button></p>}
    {latest && <>
      {latest.selection_valid === true ? <p><strong>{latest.best_round_number ? `第${latest.best_round_number}轮快照` : '快照来源轮次未记录'}</strong> · 按{model.spec.selection_metric === 'validation_sharpe' ? '验证夏普率' : '验证净对数收益'}选定</p>
        : <p className="callout warn">{latest.selection_valid === false ? '尚无有效验证评分，保留的策略快照不代表有效最优结果。' : '快照验证状态未记录。'}</p>}
      <dl className="history-key-metrics" aria-label="当前快照验证成绩">{metrics.map(([key, label]) => <div key={key}><dt id={`current-${key}`}>{label}</dt><dd aria-labelledby={`current-${key}`}>{metricText(key, latest.validation_best?.[key])}</dd></div>)}</dl>
      <p className="muted">验证区间 {model.spec.validation_start}–{model.spec.validation_end}。可在训练历史中选择其他轮次回测。</p>
      <p className="muted">最新保存：第{latest.round_number}轮 · 续训从该轮最终参数恢复。</p>
    </>}
  </section>
}
