import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, describeError } from '../api'
import { ModelCreation } from '../components/ModelCreation'
import { taskTime } from '../components/timePresentation'
import type { InvestingModel, InvestingOverview } from '../investingTypes'

export function ModelsPage() {
  const navigate = useNavigate()
  const [models, setModels] = useState<InvestingModel[]>([]), [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const [creating, setCreating] = useState(false), [overview, setOverview] = useState<InvestingOverview | null>(null)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    api.investingModels(undefined, controller.signal).then(page => {
      if (!controller.signal.aborted) { setModels(page.items); setCursor(page.next_cursor); setError('') }
    }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [refresh])
  useEffect(() => {
    if (!creating) return
    const controller = new AbortController()
    api.investingOverview(controller.signal).then(value => { if (!controller.signal.aborted) setOverview(value) }).catch(value => { if (!controller.signal.aborted) setError(describeError(value)) })
    return () => controller.abort()
  }, [creating])
  const more = async () => {
    if (!cursor) return
    setBusy(true)
    try { const page = await api.investingModels(cursor); setModels(old => [...old, ...page.items.filter(item => !old.some(value => value.id === item.id))]); setCursor(page.next_cursor); setError('') }
    catch (value) { setError(describeError(value)) }
    finally { setBusy(false) }
  }
  return <div className="page-stack models-page"><header className="page-heading section-heading"><div><h1>强化学习模型</h1><p>一个固定、有序的 ETF 篮子对应一个持久模型。追加训练积累参数，保存版本用于冻结回测。</p></div><div className="inline-actions"><button onClick={() => setRefresh(value => value + 1)} disabled={loading}>刷新模型</button><button onClick={() => setCreating(value => !value)} aria-expanded={creating}>{creating ? '收起新建模型' : '新建模型'}</button></div></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <section className="panel"><h2>我的模型</h2>{loading && <p>加载模型…</p>}{!loading && !error && !models.length && <p className="empty">尚无模型。新建模型时请明确输入研究篮子，创建后再开始训练。</p>}<div className="record-list">{models.map(model => <article key={model.id} className="record"><div className="record-head"><Link to={`/models/${encodeURIComponent(model.id)}`}>{model.name}</Link><span className="badge">{model.active_job_id ? '训练进行中' : model.version_count ? '可回测' : '待训练'}</span></div><p>{model.spec.backtest.symbols.join(' → ')} · {model.spec.frequency === 'weekly' ? '周度' : '月度'}决策</p><p className="muted">已保存累计决策步数 {model.total_timesteps.toLocaleString('zh-CN')} 步 · {model.session_count} 次训练 · {model.version_count} 个保存版本 · 创建 {taskTime(model.created_at)}</p><small>{model.id} · 数据 {model.dataset_id}</small></article>)}</div>{cursor && <button disabled={busy} onClick={() => void more()}>加载更多模型</button>}</section>
    {creating && (overview ? overview.datasets.length ? <ModelCreation overview={overview} onCreated={model => navigate(`/models/${encodeURIComponent(model.id)}`)} /> : <section className="panel"><p>暂无日线数据版本。请到 <Link to="/datasets">行情数据</Link> 准备数据。</p></section> : <p>加载日线数据与设备信息…</p>)}
  </div>
}
