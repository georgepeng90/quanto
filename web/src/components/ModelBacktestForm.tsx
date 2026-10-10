import { useEffect, useRef, useState } from 'react'
import { ApiError, api, describeError } from '../api'
import { CapabilityReport as CapabilityReportView } from './CapabilityReport'
import type { CapabilityReport, PolicyBacktestRequest } from '../frameworkTypes'
import type { InvestingModel, InvestingModelSummary, InvestingOverview, PolicyBacktestSpec, TrainingHistoryItem } from '../investingTypes'

function serverReport(error: unknown): CapabilityReport | null {
  const value = error instanceof ApiError ? error.details.report : null
  return value && typeof value === 'object' && 'status' in value ? value as CapabilityReport : null
}

export function ModelBacktestForm({ initialModel, initialVersion, onSubmitted }: { initialModel: string; initialVersion: string; onSubmitted: () => void }) {
  const [models, setModels] = useState<InvestingModel[]>([]), [modelId, setModelId] = useState(initialModel)
  const [versionId, setVersionId] = useState(initialVersion), [detail, setDetail] = useState<InvestingModelSummary | null>(null)
  const [versions, setVersions] = useState<TrainingHistoryItem[]>([]), [overview, setOverview] = useState<InvestingOverview | null>(null)
  const [datasetId, setDatasetId] = useState('')
  const [spec, setSpec] = useState<Omit<PolicyBacktestSpec, 'evaluation_purpose'>>({ start: '', end: '', baseline: 'policy', cost_multiplier: 1 })
  const [purpose, setPurpose] = useState<PolicyBacktestSpec['evaluation_purpose'] | ''>('')
  const [report, setReport] = useState<CapabilityReport | null>(null), [checkedKey, setCheckedKey] = useState('')
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState(false)
  const currentKey = JSON.stringify({ modelId, versionId, datasetId, spec, purpose })
  const latestKey = useRef(currentKey)
  latestKey.current = currentKey
  const visibleReport = checkedKey === currentKey ? report : null

  useEffect(() => {
    const controller = new AbortController()
    const load = async () => {
      try {
        const rows: InvestingModel[] = []; let cursor: string | undefined
        do {
          const page = await api.investingModels(cursor, controller.signal)
          rows.push(...page.items); cursor = page.next_cursor || undefined
        } while (cursor)
        if (!controller.signal.aborted) setModels(rows)
      } catch (value) { if (!controller.signal.aborted) setError(describeError(value)) }
    }
    void load()
    api.investingOverview(controller.signal).then(value => { if (!controller.signal.aborted) setOverview(value) })
      .catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [])
  useEffect(() => {
    setDetail(null); setVersions([]); setDatasetId(''); setReport(null); setCheckedKey(''); setNotice('')
    if (!modelId) return
    const controller = new AbortController()
    const load = async () => {
      try {
        const model = await api.investingModelSummary(modelId, controller.signal)
        const rows: TrainingHistoryItem[] = []; let anchor: number | undefined
        for (let page = 1; ; page++) {
          const history = await api.investingModelHistory(modelId, { kind: 'versions', page, page_size: 50, anchor }, controller.signal)
          rows.push(...history.items); anchor = history.anchor
          if (page >= history.total_pages) break
        }
        if (controller.signal.aborted) return
        setDetail(model); setVersions(rows); setDatasetId(model.dataset_id)
        if (!rows.some(row => row.id === versionId)) setVersionId('')
      } catch (value) { if (!controller.signal.aborted) setError(describeError(value)) }
    }
    void load()
    return () => controller.abort()
  }, [modelId])

  const request = (): PolicyBacktestRequest => {
    if (!modelId || !detail?.framework_binding) throw new Error('请选择已关联框架身份的模型。')
    if (!versionId || !versions.some(version => version.id === versionId)) throw new Error('请选择属于此模型的已保存版本。')
    if (!datasetId) throw new Error('请选择回测数据版本。')
    if (!purpose) throw new Error('请选择评估用途，开发回放不等于盲测。')
    if (!spec.start || !spec.end || spec.start >= spec.end) throw new Error('请填写有效的回测起止日期。')
    if (!Number.isFinite(spec.cost_multiplier) || spec.cost_multiplier < 0 || spec.cost_multiplier > 100)
      throw new Error('成本倍数须在 0 到 100 之间。')
    return { version_id: versionId, dataset_id: datasetId, spec: { ...spec, evaluation_purpose: purpose } }
  }
  const check = async () => {
    setError(''); setReport(null); setCheckedKey('')
    try {
      const payload = request(), key = latestKey.current
      setBusy(true)
      const next = await api.checkCapabilities({ operation: 'policy_backtest', model_id: modelId, ...payload })
      if (latestKey.current === key) { setReport(next); setCheckedKey(key) }
    } catch (value) { const next = serverReport(value); if (next) { setReport(next); setCheckedKey(latestKey.current) }; setError(describeError(value)) }
    finally { setBusy(false) }
  }
  const submit = async () => {
    if (visibleReport?.status !== 'ready') return
    setBusy(true); setError(''); setNotice('')
    try {
      const payload = request()
      const job = await api.backtestInvestingModel(modelId, payload)
      setNotice(`回测任务 ${job.id} 已提交，使用保存版本 ${versionId}。`); onSubmitted()
    } catch (value) { const next = serverReport(value); if (next) { setReport(next); setCheckedKey(latestKey.current) }; setError(describeError(value)) }
    finally { setBusy(false) }
  }
  const currentBest = versions[0]?.selection_valid ? versions[0].best_source_version_id : null

  return <section className="panel"><h2>保存版本回测</h2>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="success">{notice}</p>}
    <fieldset disabled={busy}><div className="form-grid">
      <label>强化学习模型<select value={modelId} onChange={event => { setModelId(event.target.value); setVersionId('') }}><option value="">请选择模型</option>{models.map(model => <option key={model.id} value={model.id}>{model.name} · {model.id}</option>)}</select></label>
      <label>保存版本<select value={versionId} onChange={event => setVersionId(event.target.value)}><option value="">请选择保存版本</option>{versions.map(version => <option key={version.id} value={version.id}>第 {version.round_number} 轮 · {version.id}{version.id === currentBest ? '（当前选优来源）' : ''}</option>)}</select></label>
      <label>回测数据版本<select value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">请选择数据版本</option>{detail && !overview?.datasets.some(item => item.id === detail.dataset_id) && <option value={detail.dataset_id}>{detail.dataset_id}</option>}{overview?.datasets.map(dataset => <option key={dataset.id} value={dataset.id}>{dataset.id} · {dataset.manifest.data_start}—{dataset.manifest.data_end}</option>)}</select></label>
      <label>评估用途<select value={purpose} onChange={event => setPurpose(event.target.value as typeof purpose)}><option value="">请选择评估用途</option><option value="development_replay">开发回放</option><option value="post_selection_backtest">选模后回测</option></select></label>
      <label>回测方法<select value={spec.baseline} onChange={event => setSpec({ ...spec, baseline: event.target.value as typeof spec.baseline })}><option value="policy">PPO 保存策略</option><option value="cash">现金基准</option><option value="equal_weight">等权基准</option><option value="buy_hold">买入持有基准</option></select></label>
      <label>回测开始<input type="date" value={spec.start} onChange={event => setSpec({ ...spec, start: event.target.value })} /></label>
      <label>回测结束<input type="date" value={spec.end} onChange={event => setSpec({ ...spec, end: event.target.value })} /></label>
      <label>成本倍数<input type="number" min={0} max={100} step={.1} value={spec.cost_multiplier} onChange={event => setSpec({ ...spec, cost_multiplier: Number(event.target.value) })} /></label>
    </div>{detail?.framework_binding ? <p>固定资产顺序：{detail.framework_binding.ordered_pairs.map(pair => `${pair.instrument_id.exchange}:${pair.instrument_id.instrument_type}:${pair.instrument_id.code}`).join(' → ')}</p>
      : detail && <p className="callout warn">此模型尚无可验证的框架关联，暂不可提交回测。</p>}
      <p className="muted">开发回放可用于验证区间；选模后回测须由服务端核对时间边界。选择保存版本不会改变续训所用的最近状态。</p>
      <div className="inline-actions"><button type="button" onClick={() => void check()}>检查运行能力</button><button type="button" disabled={visibleReport?.status !== 'ready'} onClick={() => void submit()}>提交模型回测</button></div>
    </fieldset>{visibleReport && <CapabilityReportView report={visibleReport} />}
  </section>
}
