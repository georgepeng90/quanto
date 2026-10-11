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
  const [assetType, setAssetType] = useState(''), [basketId, setBasketId] = useState('')
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
  const basketIds = [...new Set(models.flatMap(model => model.framework_binding ? [model.framework_binding.basket_revision_ref.basket_id] : []))]
  const shown = models.filter(model => (!assetType || model.framework_binding?.ordered_pairs.some(pair => pair.instrument_id.instrument_type === assetType))
    && (!basketId || model.framework_binding?.basket_revision_ref.basket_id === basketId))
  return <div className="page-stack models-page"><header className="page-heading section-heading"><div><h1>强化学习模型</h1><p>选择标的组合，训练模型并使用保存的训练轮次回测。</p></div><div className="inline-actions"><button onClick={() => setRefresh(value => value + 1)} disabled={loading}>刷新模型</button><button onClick={() => setCreating(value => !value)} aria-expanded={creating}>{creating ? '收起新建模型' : '新建模型'}</button></div></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <section className="panel"><h2>我的模型</h2>{loading && <p>加载模型…</p>}{!loading && !error && !models.length && <p className="empty">尚无模型。请先保存研究篮子，创建后再开始训练。</p>}
      <div className="form-grid"><label>资产类型<select value={assetType} onChange={event => setAssetType(event.target.value)}><option value="">全部</option><option value="etf">ETF</option><option value="stock">股票</option></select></label>
        <label>组合<select value={basketId} onChange={event => setBasketId(event.target.value)}><option value="">全部</option>{basketIds.map(id => <option key={id} value={id}>{id}</option>)}</select></label></div>
      {cursor && <p className="muted">筛选目前已加载的模型；可继续加载更多。</p>}
      <div className="record-list">{shown.map(model => <article key={model.id} className="record"><div className="record-head"><Link to={`/models/${encodeURIComponent(model.id)}`}>{model.name}</Link><span className="badge">{model.active_job_id ? '训练进行中' : model.version_count ? '可回测' : '待训练'}</span></div>
        <p>{model.framework_binding?.ordered_pairs.map(pair => `${pair.instrument_id.exchange}:${pair.instrument_id.code}`).join(' → ')} · {model.spec.frequency === 'weekly' ? '周度' : '月度'}决策</p>
        <p className="muted">已训练 {model.total_timesteps.toLocaleString('zh-CN')} 步 · {model.session_count} 次训练 · {model.version_count} 个保存轮次 · 创建 {taskTime(model.created_at)}</p></article>)}</div>{cursor && <button disabled={busy} onClick={() => void more()}>加载更多模型</button>}</section>
    {creating && (overview ? overview.datasets.length ? <ModelCreation overview={overview} onCreated={model => navigate(`/models/${encodeURIComponent(model.id)}`)} /> : <section className="panel"><p>暂无日线数据版本。请到 <Link to="/datasets">行情数据</Link> 准备数据。</p></section> : <p>加载日线数据与设备信息…</p>)}
  </div>
}
