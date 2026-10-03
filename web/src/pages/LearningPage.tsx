import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { artifactName, flatMetrics, JsonDetails, LearningResults, learningMetricLabels, Metrics, metricText, record, ResearchReplayLabel } from '../components/LearningResults'
import { taskTime } from '../components/timePresentation'
import type { LearningArtifact, LearningBacktestSpec, LearningOverview, TrainingSpec } from '../learningTypes'
import type { JobRecord, RunEvent } from '../types'

const trainDefaults: TrainingSpec = { name: 'GRU 模型', model_type: 'gru', train_start: '', train_end: '', validation_start: '', validation_end: '', test_start: '', test_end: '', lookback: 64, horizon: 16, epochs: 20, batch_size: 256, learning_rate: .001, hidden_size: 32, layers: 1, seed: 7, device: 'auto', patience: 5 }
const backtestDefaults: LearningBacktestSpec = { start: '', end: '', initial_cash: 1000000, top_k: 3, rebalance_every: 5, max_total_weight: .8, max_symbol_weight: .35, commission_rate: .0002, minimum_commission: 5, slippage_rate: .0005, participation_rate: .1, settlement_days: 1, lot_size: 100, drawdown_pause: .2, cash_rate: 0, baseline: 'model' }
const dateFields = ['train_start', 'train_end', 'validation_start', 'validation_end', 'test_start', 'test_end'] as const
const dateLabels = ['训练开始', '训练结束', '验证开始', '验证结束', '测试开始', '测试结束']
const trainNumbers: [keyof TrainingSpec, string, number, number][] = [['lookback', '历史窗口（K 线数）', 1, 1], ['horizon', '预测跨度（K 线数）', 1, 1], ['epochs', '训练轮数', 1, 1], ['batch_size', '批大小', 1, 1], ['learning_rate', '学习率', .000001, .0001], ['hidden_size', '隐藏维度', 1, 1], ['layers', '层数', 1, 1], ['seed', '随机种子', 0, 1], ['patience', '早停耐心轮数', 1, 1]]
const backtestNumbers: [keyof LearningBacktestSpec, string, number, number, number?][] = [['initial_cash', '初始资金', 1, 1000], ['top_k', '最多持仓数', 1, 1], ['rebalance_every', '调仓间隔（K 线数）', 1, 1], ['max_total_weight', '总仓位上限', 0, .05, 1], ['max_symbol_weight', '单标的仓位上限', 0, .05, 1], ['commission_rate', '佣金率', 0, .0001, 1], ['minimum_commission', '最低佣金', 0, 1], ['slippage_rate', '滑点率', 0, .0001, 1], ['participation_rate', '上一根成交量参与率', .0001, .01, 1], ['settlement_days', '卖出结算交易日（T+N）', 0, 1], ['lot_size', '交易整手数量', 1, 1], ['drawdown_pause', '账户回撤暂停阈值', 0, .01, 1], ['cash_rate', '现金年利率', 0, .001, 1]]
function validateNumber(value: unknown, label: string, min: number, max = Infinity, integer = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) throw new Error(`${label}必须为${integer ? '整数' : '数值'}，范围 ${min}–${max === Infinity ? '无限制' : max}。`)
}
const active = (state: string) => ['queued', 'running', 'cancel_requested'].includes(state)
const stateLabels: Record<string, string> = { queued: '排队中', running: '运行中', cancel_requested: '取消中', cancelled: '已取消', interrupted: '已中断', failed: '失败', succeeded: '成功' }
export function shanghaiUtc(value: string) {
  if (!value) throw new Error('请填写有效的时间区间（上海时间）。')
  const date = new Date(`${value}+08:00`)
  if (!Number.isFinite(date.getTime())) throw new Error('请填写有效的时间区间（上海时间）。')
  return date.toISOString()
}
function localShanghai(value: unknown) {
  if (typeof value !== 'string' || !value) return ''
  const time = new Date(value).getTime()
  return Number.isFinite(time) ? new Date(time + 8 * 3600000).toISOString().slice(0, 23).replace(/:00\.000$/, '').replace(/\.000$/, '') : ''
}
const instrumentNames = (instruments: { exchange: string; instrument_type: string; code: string }[]) => instruments.map(item => `${item.exchange}:${item.instrument_type}:${item.code}`).join('、')
function Comparison({ artifacts }: { artifacts: LearningArtifact[] }) {
  const values = (artifact: LearningArtifact) => flatMetrics(record(artifact.manifest.metrics))
  const keys = Array.from(new Set(artifacts.flatMap(artifact => Object.keys(values(artifact)))))
  if (artifacts.length < 2) return null
  return <section className="panel"><h2>{artifacts[0].kind === 'model' ? '模型指标比较' : '回测指标比较'}</h2><p className="muted">请结合完整配置核对数据、区间、成本及频率；不同条件的结果不能直接排名。</p><div className="table-wrap"><table><thead><tr><th>指标</th>{artifacts.map(item => <th key={item.id}>{artifactName(item)}<br /><ResearchReplayLabel artifact={item} /></th>)}</tr></thead><tbody>{keys.map(key => <tr key={key}><th>{learningMetricLabels[key] || key}</th>{artifacts.map(item => <td key={item.id}>{metricText(key, values(item)[key])}</td>)}</tr>)}</tbody></table></div>{artifacts.map(item => <JsonDetails key={item.id} title={`配置 · ${item.id}`} value={item.manifest} />)}</section>
}

export function LearningPage() {
  const [searchParams] = useSearchParams()
  const selectedResult = searchParams.get('backtest') || ''
  const [overview, setOverview] = useState<LearningOverview | null>(null)
  const [error, setError] = useState('')
  const [overviewError, setOverviewError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')
  const [snapshotId, setSnapshotId] = useState('')
  const [datasetName, setDatasetName] = useState('分钟训练集')
  const [frequency, setFrequency] = useState(5)
  const [datasetId, setDatasetId] = useState('')
  const [modelId, setModelId] = useState('')
  const [train, setTrain] = useState(trainDefaults)
  const [backtest, setBacktest] = useState(backtestDefaults)
  const [resumeId, setResumeId] = useState('')
  const [inSample, setInSample] = useState(false)
  const [selectedModelDetail, setSelectedModelDetail] = useState('')
  const [modelDetail, setModelDetail] = useState<LearningArtifact | null>(null)
  const [modelDetailError, setModelDetailError] = useState('')
  const [selectedLog, setSelectedLog] = useState('')
  const [logs, setLogs] = useState<RunEvent[]>([])
  const [logError, setLogError] = useState('')
  const [logLoading, setLogLoading] = useState(false)
  const [jobUpdates, setJobUpdates] = useState<JobRecord[]>([])
  const [comparisonIds, setComparisonIds] = useState<string[]>([])
  const [extraArtifacts, setExtraArtifacts] = useState<Record<string, LearningArtifact[]>>({ datasets: [], models: [], backtests: [] })
  const [listCursors, setListCursors] = useState<Record<string, number | string | null>>({})
  const [refresh, setRefresh] = useState(0)
  const alive = useRef(true)
  const originalTrainDates = useRef<Partial<Record<typeof dateFields[number], string>>>({})
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    if (!selectedModelDetail) return
    const controller = new AbortController()
    setModelDetail(null); setModelDetailError('')
    api.learningArtifact('models', selectedModelDetail, controller.signal).then(detail => { if (!controller.signal.aborted) setModelDetail(detail) }).catch(value => { if (!controller.signal.aborted) setModelDetailError(describeError(value)) })
    return () => controller.abort()
  }, [selectedModelDetail])
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const value = await api.learningOverview(controller.signal)
        if (controller.signal.aborted) return
        setOverview(value); setOverviewError('')
        setSnapshotId(previous => previous || value.snapshots[0]?.id || '')
        setDatasetId(previous => previous || value.datasets[0]?.id || '')
        setModelId(previous => previous || value.models[0]?.id || '')
        setJobUpdates(previous => previous.filter(job => !value.jobs.some(current => current.id === job.id)))
        setListCursors(previous => Object.fromEntries(['datasets', 'models', 'backtests'].map(resource => [resource, previous[resource] === undefined ? (value[resource as 'datasets'].length >= 50 ? 50 : null) : previous[resource]])))
      } catch (value) { if (!controller.signal.aborted) setOverviewError(describeError(value)) }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 3000) }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [refresh])
  const jobs = [...jobUpdates, ...(overview?.jobs || []).filter(job => !jobUpdates.some(update => update.id === job.id))]
  const logActive = jobs.some(job => job.id === selectedLog && active(job.state))
  useEffect(() => {
    if (!selectedLog) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let cursor: string | undefined
    setLogs([]); setLogError(''); setLogLoading(true)
    const load = async () => {
      try {
        const value = await api.jobLogs(selectedLog, cursor, controller.signal)
        if (controller.signal.aborted) return
        setLogs(previous => [...previous, ...value.items].filter((event, index, array) => array.findIndex(other => other.seq === event.seq) === index).slice(-500)); setLogError(''); setLogLoading(false)
        if (value.next_cursor !== null) cursor = String(value.next_cursor)
        if (value.items.length >= 100 || logActive) timer = setTimeout(load, 2000)
      } catch (value) { if (!controller.signal.aborted) { setLogError(describeError(value)); setLogLoading(false) } }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [selectedLog, logActive])
  const list = (resource: 'datasets' | 'models' | 'backtests') => [...(overview?.[resource] || []), ...extraArtifacts[resource]].filter((item, index, array) => array.findIndex(other => other.id === item.id) === index)
  const datasets = list('datasets'), models = list('models')
  const snapshotChoices = (overview?.snapshots || []).map((item, index, array) => ({ snapshot: item, key: array.findIndex(other => other.id === item.id) === index ? item.id : `${item.id}:${index}` }))
  const snapshot = snapshotChoices.find(item => item.key === snapshotId)?.snapshot
  const selectedDataset = datasets.find(item => item.id === datasetId)
  const submit = async (kind: string, operation: () => Promise<JobRecord>) => {
    setError(''); setNotice(''); setBusy(kind)
    try {
      const job = await operation()
      if (!alive.current) return
      setJobUpdates(previous => [job, ...previous.filter(item => item.id !== job.id)])
      setNotice(`任务 ${job.id} 已提交：${stateLabels[job.state] || job.state}`)
    } catch (value) { if (alive.current) setError(describeError(value)) } finally { if (alive.current) setBusy('') }
  }
  const trainPayload = () => {
    if (!datasetId) throw new Error('请选择训练数据集。')
    if (!train.name.trim()) throw new Error('请填写模型名称。')
    const spec = { ...train }
    dateFields.forEach(key => {
      const original = originalTrainDates.current[key]
      spec[key] = original && localShanghai(original) === train[key] ? original : shanghaiUtc(train[key])
    })
    const [trainStart, trainEnd, validationStart, validationEnd, testStart, testEnd] = dateFields.map(key => new Date(spec[key]).getTime())
    if (!(trainStart < trainEnd && trainEnd <= validationStart && validationStart < validationEnd && validationEnd <= testStart && testStart < testEnd)) throw new Error('训练、验证、测试时间区间必须依次排列且不重叠。')
    const maxima: Record<string, number> = { lookback: 512, horizon: 512, epochs: 1000, batch_size: 4096, learning_rate: 1, hidden_size: 512, layers: 4, seed: 2 ** 32 - 1, patience: 100 }
    trainNumbers.forEach(([key, label, min]) => validateNumber(train[key], label, min, maxima[key], key !== 'learning_rate'))
    if (train.device === 'cuda' && !overview?.capabilities.cuda_available) throw new Error('CUDA 不可用，请选择 CPU 或自动设备。')
    return { dataset_id: datasetId, spec, ...(resumeId.trim() ? { resume_job_id: resumeId.trim() } : {}) }
  }
  const backtestPayload = () => {
    if (!datasetId) throw new Error('请选择回测数据集。')
    const spec = { ...backtest, start: shanghaiUtc(backtest.start), end: shanghaiUtc(backtest.end) }
    if (spec.start >= spec.end) throw new Error('回测时间区间的开始必须早于结束。')
    backtestNumbers.forEach(([key, label, min, , max]) => validateNumber(backtest[key], label, min, max, ['top_k', 'rebalance_every', 'settlement_days', 'lot_size'].includes(key)))
    if (spec.baseline === 'model') {
      if (!modelId) throw new Error('请选择回测模型，或选择基线方法。')
      const model = models.find(item => item.id === modelId)
      if (!inSample && model?.manifest.cutoff && new Date(spec.start).getTime() < new Date(String(model.manifest.cutoff)).getTime()) throw new Error('回测开始早于模型训练/验证截止时间；请选择后续区间或显式启用样本内研究。')
    }
    return { dataset_id: datasetId, ...(spec.baseline === 'model' ? { model_id: modelId } : {}), spec, allow_in_sample: inSample }
  }
  const clone = async (model: LearningArtifact) => {
    setBusy('clone'); setError('')
    try {
      const detail = await api.learningArtifact('models', model.id)
      const config = record(detail.manifest.config)
      originalTrainDates.current = Object.fromEntries(dateFields.map(key => [key, config[key]]))
      const next = { ...trainDefaults, ...config } as TrainingSpec
      dateFields.forEach(key => { next[key] = localShanghai(config[key]) })
      setTrain(next); setResumeId('')
      const identity = record(detail.manifest.dataset)
      const reference = detail.manifest.dataset_id || identity.id
      if (typeof reference === 'string') {
        const source = datasets.find(item => item.id === reference || item.manifest.id === reference)
        if (source) setDatasetId(source.id)
      }
      setNotice(`已克隆 ${artifactName(model)} 的训练配置，可修改后提交新任务。`)
    } catch (value) { setError(describeError(value)) } finally { setBusy('') }
  }
  const resume = (job: JobRecord) => {
    const request = record(job.request), config = record(request.spec)
    originalTrainDates.current = Object.fromEntries(dateFields.map(key => [key, config[key]]))
    const next = { ...trainDefaults, ...config } as TrainingSpec
    dateFields.forEach(key => { next[key] = localShanghai(config[key]) })
    setTrain(next); setResumeId(job.id)
    if (typeof request.dataset_id === 'string') setDatasetId(request.dataset_id)
    setNotice(`恢复来源 ${job.id}；数据和配置必须一致，可调整名称及训练总轮数。`)
  }
  const more = async (resource: 'datasets' | 'models' | 'backtests') => {
    setBusy(resource); setError('')
    try {
      const page = await api.learningArtifacts(resource, listCursors[resource] || 0)
      setExtraArtifacts(previous => ({ ...previous, [resource]: [...previous[resource], ...page.items] }))
      setListCursors(previous => ({ ...previous, [resource]: page.next_cursor }))
    } catch (value) { setError(describeError(value)) } finally { setBusy('') }
  }
  const toggleCompare = (artifact: LearningArtifact) => setComparisonIds(previous => previous.includes(artifact.id) ? previous.filter(id => id !== artifact.id) : [...previous.slice(-3), artifact.id])
  const fieldDisabled = !!busy
  return <div className="page-stack learning-page">
    <header className="page-heading section-heading"><div><h1>深度学习工作台</h1><p>分钟训练集 → 训练与模型冻结 → 后续区间回测。表单与展示时间为上海时间，完整 JSON 保留 UTC；区间左闭右开。</p></div><button disabled={fieldDisabled} onClick={() => setRefresh(value => value + 1)}>刷新</button></header>
    {overviewError && <p role="alert" className="callout error">{overviewError}</p>}{error && <p role="alert" className="callout error">{error}</p>}{notice && <p role="status" className="callout success">{notice}</p>}
    {selectedResult && <LearningResults key={selectedResult} id={selectedResult} />}
    {!overview && !overviewError && <p>加载设备与制品…</p>}
    {overview && <section className="panel"><h2>运行环境</h2><p>{overview.capabilities.device_name || 'CPU'} · PyTorch {overview.capabilities.torch_version || '未安装'} · Python {overview.capabilities.python_version} · CUDA {overview.capabilities.cuda_available ? '可用' : '不可用'} · 剩余磁盘 {(overview.capabilities.disk_free_bytes / 1024 ** 3).toFixed(1)} GiB</p>{!overview.capabilities.torch_available && <p className="callout error">请按使用指南安装 learning 可选依赖后训练。</p>}<p className="muted">K 线级做多/现金研究模拟；来源与研究假设随制品保留。训练不能保证盈利。</p></section>}
    <section className="panel"><h2>1 · 准备训练集</h2>{overview && !overview.snapshots.length && <div className="callout"><p>暂无已提交的 1 分钟行情快照。现有在线供应商只提供日线。</p><p>先按标的/月拆分真实 CSV/Parquet，用下列现有脚本导入并预缓存为已提交 MarketData 快照，再刷新本页。不要把日线或合成夹具作为真实分钟来源。</p><pre>uv run --no-sync python -m scripts.import_market_data --input minute.csv --metadata import.json</pre><pre>uv run --no-sync python -m scripts.prefetch_market_data --requirement requirement.json</pre><p>import.json 需声明分钟频率、标的、价格口径、单位、来源、未知限制与时区；交易日历等需求表也需先导入。预缓存状态须为 ready。大文件按标的/月分片导入。</p></div>}
      <form onSubmit={event => { event.preventDefault(); void submit('prepare', async () => { if (!snapshot) throw new Error('请选择分钟快照。'); return api.prepareLearning({ spec: { name: datasetName, snapshot_id: snapshot.id, query: snapshot.query, frequency_minutes: frequency } }) }) }}><fieldset disabled={fieldDisabled}><div className="form-grid"><label>训练集名称<input value={datasetName} required onChange={event => setDatasetName(event.target.value)} /></label><label>分钟快照<select value={snapshotId} onChange={event => setSnapshotId(event.target.value)}><option value="">请选择快照</option>{snapshotChoices.map(({ snapshot: item, key }) => <option key={key} value={key}>{item.id} · {instrumentNames(item.instruments)} · {item.frequency}</option>)}</select></label><label>聚合频率<select value={frequency} onChange={event => setFrequency(Number(event.target.value))}>{[1, 5, 15, 30, 60].map(value => <option key={value} value={value}>{value} 分钟</option>)}</select></label></div>{snapshot && <><p>{taskTime(snapshot.start)} 至 {taskTime(snapshot.end)} · {typeof snapshot.price_basis === 'string' ? snapshot.price_basis : String(snapshot.price_basis.mode)} · {instrumentNames(snapshot.instruments)}</p><p className="muted">{snapshot.limitations.join('；')}</p><JsonDetails title="快照身份与完整数据查询" value={snapshot} /></>}<button disabled={!snapshot}>准备训练集</button></fieldset></form>
    </section>
    <section className="panel"><h2>2 · 选择训练集</h2><label>训练 / 回测数据集<select value={datasetId} onChange={event => { setDatasetId(event.target.value); setResumeId('') }}><option value="">请选择数据集</option>{datasets.map(item => <option key={item.id} value={item.id}>{artifactName(item)} · {item.id}</option>)}</select></label>{!datasets.length && <p className="empty">准备任务成功后，训练集会在此登记。</p>}{selectedDataset && <><p>{Number(selectedDataset.manifest.row_count || 0).toLocaleString('zh-CN')} 行 · {String(selectedDataset.manifest.frequency_minutes)} 分钟 · {String(selectedDataset.manifest.instruments || '')}</p><JsonDetails title="训练集覆盖、来源与限制" value={selectedDataset.manifest} /></>}{listCursors.datasets != null && <button disabled={fieldDisabled} onClick={() => void more('datasets')}>加载更多训练集</button>}</section>
    <section className="panel"><h2>3 · 配置训练</h2><p className="muted">严格按时间分区；归一化只拟合训练集，最佳权重只根据验证集选择，测试集在冻结后评估。</p><form noValidate onSubmit={event => { event.preventDefault(); void submit('train', async () => api.trainLearning(trainPayload())) }}><fieldset disabled={fieldDisabled}><div className="form-grid"><label>模型名称<input value={train.name} onChange={event => setTrain({ ...train, name: event.target.value })} /></label><label>模型类型<select value={train.model_type} onChange={event => setTrain({ ...train, model_type: event.target.value as TrainingSpec['model_type'] })}><option value="gru">GRU</option><option value="linear">线性窗口基线</option></select></label>{dateFields.map((key, index) => <label key={key}>{dateLabels[index]}（上海时间）<input type="datetime-local" step="any" value={train[key]} onChange={event => setTrain({ ...train, [key]: event.target.value })} /></label>)}<label>训练设备<select value={train.device} onChange={event => setTrain({ ...train, device: event.target.value as TrainingSpec['device'] })}><option value="auto">自动（记录实际设备）</option><option value="cpu">CPU</option><option value="cuda">CUDA（不可用则失败）</option></select></label><label>恢复检查点任务 ID（可选）<input value={resumeId} onChange={event => setResumeId(event.target.value)} /></label>{trainNumbers.map(([key, label, min, step]) => <label key={key}>{label}<input type="number" min={min} step={step} value={train[key]} onChange={event => setTrain({ ...train, [key]: Number(event.target.value) })} /></label>)}</div>{resumeId && <p className="callout">恢复创建新任务，需保持相同数据、分区和训练配置；仅名称与总轮数允许改变。</p>}<JsonDetails title="当前完整训练配置（表单时间为上海时间）" value={train} /><button disabled={!datasetId || !overview?.capabilities.torch_available}>提交训练</button></fieldset></form></section>
    <section className="panel"><h2>已冻结模型</h2>{!models.length && <p className="empty">训练成功后，模型会在此登记。</p>}<div className="record-list">{models.map(item => <article className="record" key={item.id}><div className="record-head"><strong>{artifactName(item)}</strong><small>{item.id}</small><button onClick={() => setSelectedModelDetail(item.id)}>查看模型 {item.id}</button><button disabled={fieldDisabled} onClick={() => void clone(item)}>克隆配置 {item.id}</button><label><input type="checkbox" checked={comparisonIds.includes(item.id)} onChange={() => toggleCompare(item)} />比较模型 {item.id}</label></div><p>训练/验证信息截止：{taskTime(typeof item.manifest.cutoff === 'string' ? item.manifest.cutoff : null)}</p><Metrics values={record(item.manifest.metrics)} /><JsonDetails title="模型完整配置与训练曲线" value={item.manifest} /></article>)}</div>{listCursors.models != null && <button disabled={fieldDisabled} onClick={() => void more('models')}>加载更多模型</button>}</section>
    <section className="panel"><h2>4 · 模型 / 基线回测</h2><form noValidate onSubmit={event => { event.preventDefault(); void submit('backtest', async () => api.backtestLearning(backtestPayload())) }}><fieldset disabled={fieldDisabled}><div className="form-grid"><label>回测方法<select value={backtest.baseline} onChange={event => setBacktest({ ...backtest, baseline: event.target.value as LearningBacktestSpec['baseline'] })}><option value="model">模型排名</option><option value="equal_weight">等权基线</option><option value="momentum">动量基线</option><option value="cash">现金基线</option></select></label><label>回测模型<select value={modelId} disabled={backtest.baseline !== 'model'} onChange={event => setModelId(event.target.value)}><option value="">请选择模型</option>{models.map(item => <option key={item.id} value={item.id}>{artifactName(item)} · {item.id}</option>)}</select></label>{(['start', 'end'] as const).map((key, index) => <label key={key}>{index === 0 ? '回测开始' : '回测结束'}（上海时间）<input type="datetime-local" step="any" value={backtest[key]} onChange={event => setBacktest({ ...backtest, [key]: event.target.value })} /></label>)}{backtestNumbers.map(([key, label, min, step, max]) => <label key={key}>{label}<input type="number" min={min} max={max} step={step} value={backtest[key]} onChange={event => setBacktest({ ...backtest, [key]: Number(event.target.value) })} /></label>)}</div><label><input type="checkbox" checked={inSample} onChange={event => setInSample(event.target.checked)} />允许样本内研究（非独立留出证据）</label>{inSample && <p className="callout error">样本内回放可能包含已参与模型选择的历史，不可当作独立的新留出证据。</p>}<p className="muted">信号后下一根开盘成交；T+N 按交易日推进。整手、成本和参与率为显式研究假设。</p><JsonDetails title="当前完整回测配置" value={{ ...backtest, dataset_id: datasetId, model_id: modelId, allow_in_sample: inSample }} /><button disabled={!datasetId || (backtest.baseline === 'model' && !modelId)}>提交回测</button></fieldset></form></section>
    <section className="panel"><h2>任务进度与日志</h2>{!jobs.length && <p className="empty">暂无学习任务。</p>}<div className="record-list">{jobs.map(job => <article key={job.id} className="record"><div className="record-head"><strong>{job.id}</strong><span>{job.kind}</span><span className="badge">{stateLabels[job.state] || job.state}</span><button onClick={() => setSelectedLog(job.id)}>查看日志 {job.id}</button>{active(job.state) && <button disabled={fieldDisabled || job.state === 'cancel_requested'} onClick={() => void submit('cancel', () => api.cancelJob(job.id))}>取消 {job.id}</button>}{job.kind === 'train' && job.checkpoint_available && ['failed', 'interrupted', 'cancelled'].includes(job.state) && <button onClick={() => resume(job)}>恢复配置 {job.id}</button>}</div><p className="muted">完成至 {job.completed_through || '—'} · 心跳 {taskTime(job.heartbeat)} · 开始 {taskTime(job.started_at)}</p>{job.error && <p className="error">{job.error}</p>}</article>)}</div>{selectedLog && <div className="callout"><h3>任务日志 · {selectedLog}</h3>{logLoading && <p>加载日志…</p>}{logError && <p role="alert" className="error">{logError}</p>}<p className="muted">保留最近 500 条事件；日志时间为上海时间。</p><ol className="event-timeline">{logs.map((event, index) => <li key={`${event.seq}-${index}`}><small>{event.phase} · {taskTime(event.asof)}</small><p>{event.message}</p></li>)}</ol>{!logLoading && !logs.length && !logError && <p>暂无日志。</p>}</div>}</section>
    <section className="panel"><h2>回测记录</h2><p>在 <Link to="/runs">全部回测</Link> 查看状态、指标和跨方法比较；选择一条分钟模型回测可在此打开完整结果。</p></section>
    {selectedModelDetail && <section className="panel"><h2>模型详情 · {selectedModelDetail}</h2>{modelDetail ? <><Metrics values={record(modelDetail.manifest.metrics)} /><JsonDetails title="实际设备、来源与完整模型配置" value={modelDetail.manifest} />{Array.isArray(modelDetail.manifest.history) && <><h3>训练曲线</h3><div className="table-wrap"><table><thead><tr><th>轮次</th><th>训练损失</th><th>验证损失</th></tr></thead><tbody>{modelDetail.manifest.history.map((item, index) => { const row = record(item); return <tr key={index}><td>{String(row.epoch ?? index + 1)}</td><td>{metricText('loss', row.train_loss ?? row.train_mse)}</td><td>{metricText('loss', row.validation_loss ?? row.validation_mse)}</td></tr> })}</tbody></table></div></>}</> : modelDetailError ? <p role="alert" className="error">{modelDetailError}</p> : <p>加载模型详情…</p>}</section>}
    <Comparison artifacts={models.filter(item => comparisonIds.includes(item.id))} />
  </div>
}
