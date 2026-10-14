import { useEffect, useId, useRef, useState } from 'react'
import { ApiError, api, describeError } from '../api'
import { CapabilityReport as CapabilityReportView } from './CapabilityReport'
import type { CapabilityReport, PolicyBacktestPreview, PolicyBacktestRequest } from '../frameworkTypes'
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
  const [report, setReport] = useState<CapabilityReport | null>(null), [checkedKey, setCheckedKey] = useState('')
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [busy, setBusy] = useState<'checking' | 'submitting' | null>(null)
  const [confirmation, setConfirmation] = useState<{ key: string; payload: PolicyBacktestRequest; preview: PolicyBacktestPreview } | null>(null)
  const currentKey = JSON.stringify({ modelId, versionId, datasetId, spec })
  const latestKey = useRef(currentKey)
  const flight = useRef<AbortController | null>(null)
  const dialog = useRef<HTMLDialogElement>(null), startButton = useRef<HTMLButtonElement>(null)
  const dialogTitle = useId(), dialogDescription = useId()
  latestKey.current = currentKey
  const visibleReport = checkedKey === currentKey ? report : null
  const visibleConfirmation = confirmation?.key === currentKey ? confirmation : null

  useEffect(() => () => { flight.current?.abort() }, [])
  useEffect(() => {
    if (visibleConfirmation) dialog.current?.showModal()
    else if (dialog.current?.open) { dialog.current.close(); startButton.current?.focus() }
  }, [visibleConfirmation])

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
    setDetail(null); setVersions([]); setDatasetId(''); setReport(null); setCheckedKey(''); setNotice(''); setError(''); setConfirmation(null)
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
    if (!spec.start || !spec.end || spec.start >= spec.end) throw new Error('请填写有效的回测起止日期。')
    if (!Number.isFinite(spec.cost_multiplier) || spec.cost_multiplier < 0 || spec.cost_multiplier > 100)
      throw new Error('成本倍数须在 0 到 100 之间。')
    return { version_id: versionId, dataset_id: datasetId, spec: { ...spec } }
  }
  const run = async (confirmed?: PolicyBacktestRequest) => {
    if (flight.current) return
    const controller = new AbortController(), key = latestKey.current
    const isCurrent = () => !controller.signal.aborted && latestKey.current === key
    flight.current = controller
    setError(''); setNotice(''); setReport(null); setCheckedKey('')
    try {
      const payload = confirmed || request()
      if (!confirmed) {
        setBusy('checking')
        const preview = await api.previewInvestingModelBacktest(modelId, payload, controller.signal)
        if (!isCurrent()) return
        setReport(preview.report); setCheckedKey(key)
        if (!preview.available_interval || preview.report.status !== 'ready') {
          setError(preview.reasons.join('；') || '所选区间没有可回测数据。'); return
        }
        if (preview.requires_confirmation) { setConfirmation({ key, payload, preview }); return }
      }
      setBusy('submitting')
      await api.backtestInvestingModel(modelId, payload, controller.signal)
      if (isCurrent()) { setNotice('模型回测已提交。'); onSubmitted() }
    } catch (value) {
      if (isCurrent()) {
        const next = serverReport(value)
        if (next) { setReport(next); setCheckedKey(key) }
        setError(describeError(value))
      }
    } finally { if (flight.current === controller) { flight.current = null; if (!controller.signal.aborted) setBusy(null) } }
  }
  const confirm = () => {
    if (!visibleConfirmation?.preview.available_interval || flight.current) return
    const { start, end } = visibleConfirmation.preview.available_interval
    const payload = { ...visibleConfirmation.payload, spec: { ...visibleConfirmation.payload.spec, start, end } }
    const nextSpec = { ...spec, start, end }
    latestKey.current = JSON.stringify({ modelId, versionId, datasetId, spec: nextSpec })
    setSpec(nextSpec); setConfirmation(null)
    void run(payload)
  }
  const currentBest = versions[0]?.selection_valid ? versions[0].best_source_version_id : null

  return <section className="panel"><h2>保存版本回测</h2>
    {error && <p role="alert" className="error">{error}</p>}{notice && <p role="status" className="success">{notice}</p>}
    <fieldset disabled={!!busy || !!visibleConfirmation}><div className="form-grid">
      <label>强化学习模型<select value={modelId} onChange={event => { setModelId(event.target.value); setVersionId('') }}><option value="">请选择模型</option>{models.map(model => <option key={model.id} value={model.id}>{model.name}</option>)}</select></label>
      <label>训练轮次<select value={versionId} onChange={event => setVersionId(event.target.value)}><option value="">请选择训练轮次</option>{versions.map(version => <option key={version.id} value={version.id}>第 {version.round_number} 轮{version.id === currentBest ? '（当前选优来源）' : ''}</option>)}</select></label>
      <label>回测数据<select value={datasetId} onChange={event => setDatasetId(event.target.value)}><option value="">请选择数据</option>{detail && !overview?.datasets.some(item => item.id === detail.dataset_id) && <option value={detail.dataset_id}>训练所用数据</option>}{overview?.datasets.map(dataset => <option key={dataset.id} value={dataset.id}>{dataset.manifest.source} · {dataset.manifest.data_start}—{dataset.manifest.data_end}</option>)}</select></label>
      <label>回测方法<select value={spec.baseline} onChange={event => setSpec({ ...spec, baseline: event.target.value as typeof spec.baseline })}><option value="policy">PPO 保存策略</option><option value="cash">现金基准</option><option value="equal_weight">等权基准</option><option value="buy_hold">买入持有基准</option></select></label>
      <label>回测开始<input type="date" value={spec.start} onChange={event => setSpec({ ...spec, start: event.target.value })} /></label>
      <label>回测结束<input type="date" value={spec.end} onChange={event => setSpec({ ...spec, end: event.target.value })} /></label>
      <label>成本倍数<input type="number" min={0} max={100} step={.1} value={spec.cost_multiplier} onChange={event => setSpec({ ...spec, cost_multiplier: Number(event.target.value) })} /></label>
    </div>{detail?.framework_binding && <p>标的组合：{detail.framework_binding.ordered_pairs.map(pair => `${pair.instrument_id.exchange}:${pair.instrument_id.code}`).join(' → ')}</p>}
      <p className="muted">开始回测时将检查日期；如需调整，会先请你确认可用区间。</p>
      <div className="inline-actions"><button ref={startButton} type="button" onClick={() => void run()}>{busy === 'checking' ? '检查日期中…' : busy === 'submitting' ? '提交中…' : '开始回测'}</button></div>
    </fieldset>{visibleReport && <CapabilityReportView report={visibleReport} />}
    <dialog ref={dialog} className="backtest-confirmation" aria-labelledby={dialogTitle} aria-describedby={dialogDescription} onCancel={event => { event.preventDefault(); setConfirmation(null) }}>
      {visibleConfirmation && <><h2 id={dialogTitle}>确认回测区间</h2><div id={dialogDescription}>
        <p>输入区间：{visibleConfirmation.preview.requested_interval.start}—{visibleConfirmation.preview.requested_interval.end}</p>
        <p>输入范围内最大可回测区间：{visibleConfirmation.preview.available_interval?.start}—{visibleConfirmation.preview.available_interval?.end}（{visibleConfirmation.preview.available_interval?.trading_sessions} 个交易日）</p>
        {visibleConfirmation.preview.reasons.map((reason, index) => <p key={index}>{reason}</p>)}
        <p>是否使用此区间开始回测？</p>
      </div><div className="inline-actions"><button type="button" onClick={confirm}>确认并回测</button><button type="button" autoFocus onClick={() => setConfirmation(null)}>取消</button></div></>}
    </dialog>
  </section>
}
