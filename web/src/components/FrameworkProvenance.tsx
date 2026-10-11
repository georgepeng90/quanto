import { JsonDetails, record } from './LearningResults'

function identity(value: unknown): string {
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
  const framework = record(value?.framework), execution = record(value?.execution_provenance)
  const pairs = Array.isArray(framework.ordered_pairs) ? framework.ordered_pairs.map(record) : []
  if (!pairs.length) return null
  const basket = record(framework.basket), benchmark = record(record(framework.asset_binding).benchmark)
  const request = record(framework.request), spec = record(request.spec)
  const purpose = String(value?.evaluation_purpose || spec.evaluation_purpose || '')
  const boundaries = record(execution.training_boundaries)
  const best = record(execution.best_checkpoint), last = record(execution.last_checkpoint)
  return <div className="framework-provenance">
    <p>标的组合：{pairs.map(pair => identity(pair.instrument_id)).join(' → ')}</p>
    <p>基准 {benchmark.instrument_id ? identity(benchmark.instrument_id) : String(benchmark.custom_series_id || '未知')} · {basisLabels[String(benchmark.return_basis)] || '收益口径未知'}</p>
    {purpose && <p>{purposeLabels[purpose] || purpose}</p>}
    {request.sample_purpose != null && <p>研究用途：{String(request.sample_purpose)}</p>}
    {!!Object.keys(boundaries).length && <><p>训练 {String(boundaries.train_start)} — {String(boundaries.train_end)}；验证 {String(boundaries.validation_start)} — {String(boundaries.validation_end)}</p><p>{boundaries.test_start ? `预留测试区间 ${boundaries.test_start} — ${boundaries.test_end}；日期声明不代表已有测试成绩。` : '未设置测试区间；测试成绩未评估。'}</p></>}
    {!!Object.keys(best).length && <p>选定快照：决策步 {String(best.timesteps)}</p>}
    {!!Object.keys(last).length && <p>末次检查点：累计决策步 {String(last.cumulative_timesteps)} · 本次 {String(last.session_timesteps)}，用于继续训练。</p>}
    <JsonDetails title="诊断信息" value={{ basket, request_hash: framework.request_hash, definitions: framework.definitions, execution_provenance: execution }} />
  </div>
}
