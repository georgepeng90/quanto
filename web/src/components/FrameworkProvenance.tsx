import { JsonDetails, record } from './LearningResults'

function identity(value: unknown): string {
  if (typeof value === 'string') {
    const parts = value.split(':')
    return parts.length === 3 ? `${parts[0]}:${parts[2]}` : value
  }
  const item = record(value)
  return ['exchange', 'instrument_type', 'code'].every(key => typeof item[key] === 'string' && item[key])
    ? `${item.exchange}:${item.code}` : ''
}
const purposeLabels: Record<string, string> = {
  development_replay: '开发回放 · 使用过的研究区间',
  post_selection_backtest: '后选模回测 · 独立性仍需研究记录证明',
  model_selection: '内部验证选模',
}
const basisLabels: Record<string, string> = { price_index: '价格指数', total_return: '全收益', other: '其他收益口径' }

export function FrameworkProvenance({ value }: { value?: Record<string, unknown> }) {
  const context = Array.isArray(value?.ordered_pairs) ? record(value) : record(value?.framework)
  const execution = record(value?.execution_provenance)
  const pairs = Array.isArray(context.ordered_pairs) ? context.ordered_pairs.map(record) : []
  if (!pairs.length) return null
  const basket = record(context.basket), benchmark = record(record(context.asset_binding).benchmark)
  const request = record(context.request), spec = record(request.spec)
  const purpose = String(value?.evaluation_purpose || spec.evaluation_purpose || '')
  const currentBenchmark = record(context.benchmark_identity)
  const benchmarkIdentity = currentBenchmark.identity || context.benchmark_identity || benchmark.instrument_id || benchmark.custom_series_id
  const benchmarkName = typeof benchmarkIdentity === 'string' && !benchmarkIdentity.includes(':') ? '自定义基准' : identity(benchmarkIdentity)
  const benchmarkBasis = currentBenchmark.return_basis || benchmark.return_basis
  const samplePurpose = value?.sample_purpose ?? request.sample_purpose
  const boundaries = record(execution.training_boundaries)
  const best = record(execution.best_checkpoint), last = record(execution.last_checkpoint)
  return <div className="framework-provenance">
    <p>标的组合：{pairs.map(pair => identity(pair.instrument_id)).join(' → ')}</p>
    {Boolean(benchmarkIdentity) && <p>基准 {benchmarkName}{benchmarkBasis ? ` · ${basisLabels[String(benchmarkBasis)] || String(benchmarkBasis)}` : ''}</p>}
    {purpose && <p>{purposeLabels[purpose] || purpose}</p>}
    {samplePurpose != null && <p>研究用途：{String(samplePurpose)}</p>}
    {!!Object.keys(boundaries).length && <><p>训练 {String(boundaries.train_start)} — {String(boundaries.train_end)}；验证 {String(boundaries.validation_start)} — {String(boundaries.validation_end)}</p><p>{boundaries.test_start ? `预留测试区间 ${boundaries.test_start} — ${boundaries.test_end}；日期声明不代表已有测试成绩。` : '未设置测试区间；测试成绩未评估。'}</p></>}
    {!!Object.keys(best).length && <p>选定快照：决策步 {String(best.timesteps)}</p>}
    {!!Object.keys(last).length && <p>末次检查点：累计决策步 {String(last.cumulative_timesteps)} · 本次 {String(last.session_timesteps)}，用于继续训练。</p>}
    <JsonDetails title="诊断信息" value={{ basket, context, execution_provenance: execution }} />
  </div>
}
