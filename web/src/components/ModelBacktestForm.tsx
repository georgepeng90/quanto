import { useEffect, useState } from 'react'
import { api, describeError } from '../api'
import type { InvestingModel, InvestingModelDetail, InvestingOverview, PolicyBacktestSpec } from '../investingTypes'

export function ModelBacktestForm({ initialModel, initialVersion, onSubmitted }: { initialModel: string; initialVersion: string; onSubmitted: () => void }) {
  const [models, setModels] = useState<InvestingModel[]>([]), [modelId, setModelId] = useState(initialModel), [versionId, setVersionId] = useState(initialVersion)
  const [detail, setDetail] = useState<InvestingModelDetail | null>(null), [overview, setOverview] = useState<InvestingOverview | null>(null)
  const [datasetId, setDatasetId] = useState(''), [spec, setSpec] = useState<PolicyBacktestSpec>({ start: '', end: '', baseline: 'policy', cost_multiplier: 1 })
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    const load = async () => {
      try {
        const rows: InvestingModel[] = []; let cursor: string | undefined
        for (;;) {
          const page = await api.investingModels(cursor, controller.signal)
          rows.push(...page.items)
          if (!page.next_cursor) break
          if (cursor === page.next_cursor) throw new Error('模型列表游标未前进。')
          cursor = page.next_cursor
        }
        if (!controller.signal.aborted) setModels(rows)
      } catch (value) { if (!controller.signal.aborted) setError(describeError(value)) }
    }
    void load()
    api.investingOverview(controller.signal).then(value => { if (!controller.signal.aborted) setOverview(value) }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [])
  useEffect(() => {
    setDetail(null); setDatasetId(''); setNotice(''); setError(''); setSpec(old => ({ ...old, start: '', end: '' }))
    if (!modelId) return
    const controller = new AbortController()
    api.investingModel(modelId, controller.signal).then(model => {
      if (controller.signal.aborted) return
      setDetail(model); setDatasetId(model.dataset_id)
      setVersionId(old => old || model.latest_version_id || '')
      setSpec(old => ({ ...old, start: model.spec.test_start ?? '', end: model.spec.test_end ?? '' }))
    }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [modelId])
  const submit = async () => {
    setBusy(true); setError(''); setNotice('')
    try {
      if (!detail || !versionId || !detail.versions.some(version => version.id === versionId)) throw new Error('请选择属于此模型的保存版本。')
      if (!datasetId) throw new Error('请选择日线数据版本。')
      if (!/^\d{4}-\d{2}-\d{2}$/.test(spec.start) || !/^\d{4}-\d{2}-\d{2}$/.test(spec.end) || spec.start >= spec.end) throw new Error('请填写有效的回测起止日期，开始必须早于结束。')
      if (spec.start <= detail.spec.validation_end) throw new Error('回测开始必须晚于模型验证截止日期。')
      if (!Number.isFinite(spec.cost_multiplier) || spec.cost_multiplier < 0 || spec.cost_multiplier > 100) throw new Error('成本倍数必须在 0–100 之间。')
      const job = await api.backtestInvestingModel(modelId, { version_id: versionId, dataset_id: datasetId, spec })
      setNotice(`回测任务 ${job.id} 已提交，使用保存版本 ${versionId}。`); onSubmitted()
    } catch (value) { setError(describeError(value)) }
    finally { setBusy(false) }
  }
  return <section className="panel"><h2>保存版本回测</h2><p className="muted">默认使用模型当前选定快照，也可选择其他轮次的保存版本。</p>{error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="success">{notice}</p>}<form noValidate onSubmit={event => { event.preventDefault(); void submit() }}><fieldset disabled={busy}><div className="form-grid"><label>强化学习模型<select value={modelId} onChange={event => { setModelId(event.target.value); setVersionId('') }}><option value="">请选择模型</option>{models.map(model => <option key={model.id} value={model.id}>{model.name} · {model.id}</option>)}</select></label><label>保存版本<select value={versionId} onChange={event => setVersionId(event.target.value)}><option value="">请选择保存版本</option>{detail?.versions.map(version => <option key={version.id} value={version.id}>第{version.round_number}轮保存 · {version.best_round_number ? `选定快照：第${version.best_round_number}轮` : '快照来源未记录'} · 累计 {String(version.manifest.timesteps_completed ?? '—')} 步{version.id === detail.latest_version_id ? ' · 当前快照（默认）' : ''}</option>)}</select></label><label>回测日线数据版本<select value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">请选择数据版本</option>{detail && !overview?.datasets.some(item => item.id === detail.dataset_id) && <option value={detail.dataset_id}>{detail.dataset_id} · 原始数据版本</option>}{overview?.datasets.map(dataset => <option key={dataset.id} value={dataset.id}>{dataset.id} · {dataset.manifest.data_start}–{dataset.manifest.data_end}</option>)}</select></label><label>回测方法<select value={spec.baseline} onChange={event => setSpec({ ...spec, baseline: event.target.value as PolicyBacktestSpec['baseline'] })}><option value="policy">PPO 冻结策略</option><option value="cash">现金基准</option><option value="equal_weight">等权基准</option><option value="buy_hold">买入持有基准</option></select></label>{(['start', 'end'] as const).map((key, index) => <label key={key}>{index ? '回测结束' : '回测开始'}<input type="date" value={spec[key]} onChange={event => setSpec({ ...spec, [key]: event.target.value })} /></label>)}<label>成本倍数<input type="number" min={0} max={100} step={.1} value={spec.cost_multiplier} onChange={event => setSpec({ ...spec, cost_multiplier: Number(event.target.value) })} /></label></div>
      {detail && <><p>固定篮子：{detail.spec.backtest.symbols.join(' → ')}</p><p className="muted">默认使用原始数据版本，可选择覆盖合格且身份一致的新日线版本；服务端校验身份与覆盖范围。回测只读取所选版本，继续训练不影响此回测。</p>{!detail.versions.length && <p>此模型尚无成功保存版本，请先完成训练。</p>}{versionId && !detail.versions.some(version => version.id === versionId) && <p className="error">指定版本不属于此模型或不存在，请重新选择。</p>}</>}
      <button disabled={!modelId || !detail}>提交模型回测</button></fieldset></form></section>
}
