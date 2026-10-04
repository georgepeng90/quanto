import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { JsonDetails, Metrics, record } from '../components/LearningResults'
import { taskTime } from '../components/timePresentation'
import type { InvestingModelDetail } from '../investingTypes'
import type { RunEvent } from '../types'

const states: Record<string, string> = { queued: '排队中', running: '运行中', cancel_requested: '取消中', cancelled: '已取消', interrupted: '已中断', failed: '失败', succeeded: '成功' }
export function ModelDetailPage() {
  const { id = '' } = useParams()
  const [model, setModel] = useState<InvestingModelDetail | null>(null), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0), [budget, setBudget] = useState(4096), [device, setDevice] = useState<'cpu' | 'cuda'>('cpu')
  const [cudaAvailable, setCudaAvailable] = useState(false), [selectedLog, setSelectedLog] = useState(''), [logs, setLogs] = useState<RunEvent[]>([]), [logError, setLogError] = useState('')
  const initialized = useRef('')
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const value = await api.investingModel(id, controller.signal)
        if (controller.signal.aborted) return
        setModel(value); setError('')
        if (initialized.current !== id) { setBudget(value.spec.total_timesteps); setDevice(value.spec.device); initialized.current = id }
        if (value.active_job_id) timer = setTimeout(load, 3000)
      } catch (value) { if (!controller.signal.aborted) setError(describeError(value)) }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [id, refresh])
  useEffect(() => {
    const controller = new AbortController()
    api.investingOverview(controller.signal).then(value => { if (!controller.signal.aborted) setCudaAvailable(value.capabilities.cuda_available) }).catch(() => {})
    return () => controller.abort()
  }, [])
  const logActive = model?.sessions.some(job => job.id === selectedLog && ['queued', 'running', 'cancel_requested'].includes(job.state))
  useEffect(() => {
    if (!selectedLog) return
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>; let cursor: string | undefined
    setLogs([]); setLogError('')
    const load = async () => {
      try {
        const page = await api.jobLogs(selectedLog, cursor, controller.signal)
        if (controller.signal.aborted) return
        setLogs(old => [...old, ...page.items].filter((event, index, array) => array.findIndex(other => other.seq === event.seq) === index).slice(-500)); setLogError('')
        if (page.next_cursor !== null) cursor = page.next_cursor
        if (page.items.length >= 100 || logActive) timer = setTimeout(load, 2000)
      } catch (value) { if (!controller.signal.aborted) setLogError(describeError(value)) }
    }
    void load()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [selectedLog, logActive])
  const train = async () => {
    if (!model || model.active_job_id || busy) return
    setError(''); setBusy(true)
    try {
      if (!Number.isInteger(budget) || budget < 2 || budget > 10000000) throw new Error('追加决策步数必须为 2–10000000 的整数。')
      const job = await api.trainInvestingModel(id, { additional_timesteps: budget, device })
      setModel(old => old && ({ ...old, active_job_id: job.id, sessions: [{ ...job, base_version_id: old.latest_version_id }, ...old.sessions] }))
      setNotice(`训练会话 ${job.id} 已提交。`); setRefresh(value => value + 1)
    } catch (value) { setError(describeError(value)) }
    finally { setBusy(false) }
  }
  const cancel = async () => {
    if (!model?.active_job_id || busy) return
    setBusy(true); setError('')
    try {
      const job = await api.cancelJob(model.active_job_id)
      setModel(old => old && ({ ...old, sessions: old.sessions.map(value => value.id === job.id ? { ...value, ...job } : value) }))
      setNotice('已请求取消；等待会话停止。本次未发布进度将丢弃。'); setRefresh(value => value + 1)
    } catch (value) { setError(describeError(value)) }
    finally { setBusy(false) }
  }
  return <div className="page-stack models-page"><header className="page-heading section-heading"><div><Link to="/models">全部模型</Link><h1>{model?.name || '模型详情'}</h1></div><button onClick={() => setRefresh(value => value + 1)} disabled={busy}>刷新模型</button></header>{error && <p role="alert" className="error callout">{error}</p>}{notice && <p role="status" className="callout success">{notice}</p>}{!model && !error && <p>加载模型…</p>}
    {model && <><section className="panel"><h2>固定研究条件</h2><p className="model-basket">{model.spec.backtest.symbols.join(' → ')}</p><p>数据 {model.dataset_id} · {model.spec.frequency === 'weekly' ? '自然周' : '自然月'}决策 · 已保存累计决策步数 {model.total_timesteps.toLocaleString('zh-CN')} 步</p><p>训练 {model.spec.train_start}–{model.spec.train_end} · 验证 {model.spec.validation_start}–{model.spec.validation_end} · 留出测试 {model.spec.test_start}–{model.spec.test_end}</p><p>选模：{model.spec.selection_metric === 'validation_sharpe' ? '验证夏普率' : '验证净对数收益'}</p><p className="muted">篮子顺序、观察窗口、网络、日期、成本和风控均已固定。修改研究条件需新建模型。训练过程不自动评估测试段。</p><JsonDetails title="完整固定配置" value={model.spec} /></section>
    <section className="panel"><h2>{model.version_count ? '追加训练' : '开始训练'}</h2>{!model.version_count && <p className="empty">待训练：尚无保存版本。首次训练成功后才能回测。</p>}<form noValidate onSubmit={event => { event.preventDefault(); void train() }}><fieldset disabled={busy || !!model.active_job_id}><div className="form-grid"><label>追加决策步数<input type="number" min={2} max={10000000} step={1} value={budget} onChange={event => setBudget(Number(event.target.value))} /></label><label>训练设备<select value={device} onChange={event => setDevice(event.target.value as 'cpu' | 'cuda')}><option value="cpu">CPU</option><option value="cuda" disabled={!cudaAvailable}>CUDA</option></select></label></div><p className="muted">每次追加有限步数，反复采样历史片段；成功后可继续追加。续训恢复上次成功版本的最终参数与优化器。</p><button>{model.version_count ? '继续训练' : '开始训练'}</button></fieldset></form>{model.active_job_id && <div className="callout"><p>当前会话 {model.active_job_id} · 同一模型同时只能有一次训练。</p><p>取消会丢弃本次未发布进度，已有版本继续可用；下次从上次成功版本继续。</p><button disabled={busy || model.sessions.some(job => job.id === model.active_job_id && job.state === 'cancel_requested')} onClick={() => void cancel()}>取消当前训练</button></div>}</section>
    <section className="panel"><h2>保存版本 · {model.version_count}</h2><p className="muted">回测使用该版本保存的全局验证最优策略；续训使用该版本的最终训练状态。已有版本不会被后续训练改写。缺失夏普率显示为 —。</p>{!model.versions.length && <p>暂无版本。失败或取消的会话不会发布新版本。</p>}<div className="record-list">{model.versions.map(version => <article className="record" key={version.id}><div className="record-head"><strong>{version.id}</strong><span>{taskTime(version.created_at)}</span><Link to={`/runs?model=${encodeURIComponent(id)}&version=${encodeURIComponent(version.id)}`}>使用此版本回测</Link></div><p>累计步数 {String(version.manifest.timesteps_completed ?? '—')} · 最优策略步数 {String(version.manifest.best_timesteps ?? '—')}</p>{version.manifest.selection_valid === false && <p className="callout warn">无有效验证评分：选模指标无定义，此版本仅保留一个可用策略快照。</p>}<h3>验证段指标</h3><Metrics values={record(record(version.manifest.metrics).validation)} /><JsonDetails title="验证轮次与版本完整信息" value={version.manifest} /></article>)}</div></section>
    <section className="panel"><h2>训练会话 · {model.session_count}</h2>{!model.sessions.length && <p>尚未提交训练。</p>}<div className="record-list">{model.sessions.map(job => <article className="record" key={job.id}><div className="record-head"><strong>{job.id}</strong><span className="badge">{states[job.state] || job.state}</span><button onClick={() => setSelectedLog(job.id)}>查看日志 {job.id}</button></div><p className="muted">基础版本 {job.base_version_id || '首次初始化'} · 进度 {job.completed_through || '—'} · 心跳 {taskTime(job.heartbeat)}</p>{job.error && <p className="error">{job.error}</p>}</article>)}</div>{selectedLog && <div><h3>任务日志 · {selectedLog}</h3>{logError && <p role="alert" className="error">{logError}</p>}<ol className="event-timeline">{logs.map(event => <li key={event.seq}><small>{event.phase}</small><p>{event.message}</p>{typeof event.payload.progress === 'number' && <progress aria-label="训练会话进度" max={1} value={event.payload.progress} />}{event.payload.timesteps !== undefined && <small>累计步数 {String(event.payload.timesteps)}</small>}{event.payload.validation_sharpe !== undefined && <small> 验证夏普 {String(event.payload.validation_sharpe ?? '—')}</small>}</li>)}</ol><p className="muted">保留最近 500 条事件。</p></div>}</section></>}
  </div>
}
