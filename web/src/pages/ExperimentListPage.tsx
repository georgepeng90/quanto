import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, describeError } from '../api'
import { instrumentKey, type FrozenBasketRevision } from '../frameworkTypes'
import type { ExperimentDraft, StrategyRevision } from '../types'

const describeValue = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value)
const compactValue = (value: unknown) => { const text = describeValue(value); return text.length > 40 ? `${text.slice(0, 40)}…` : text }

export function ExperimentListPage() {
  const [drafts, setDrafts] = useState<ExperimentDraft[]>([])
  const [strategies, setStrategies] = useState<Record<string, StrategyRevision>>({})
  const [baskets, setBaskets] = useState<Record<string, { name: string; revision: FrozenBasketRevision }>>({})
  const [assetFilter, setAssetFilter] = useState(''), [basketFilter, setBasketFilter] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)

  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    Promise.all([api.experiments(), api.strategies().catch(() => ({ items: [] as StrategyRevision[], next_cursor: null }))]).then(async ([saved, source]) => {
      const known = Object.fromEntries(source.items.map(item => [item.id, item])) as Record<string, StrategyRevision>
      const missing = [...new Set(saved.items.map(item => item.payload.strategy_revision_id))].filter(id => id && !known[id])
      await Promise.all(missing.map(async id => { try { known[id] = await api.strategy(id) } catch { /* Keep the saved configuration visible without its strategy metadata. */ } }))
      if (!active) return
      setStrategies(known)
      setDrafts(saved.items)
      setCursor(saved.next_cursor)
      await loadBaskets(saved.items)
    }).catch(reason => { if (active) setError(describeError(reason)) }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [refresh])

  const loadBaskets = async (items: ExperimentDraft[]) => {
    const refs = items.map(item => item.payload.basket_revision_ref)
    const unique = [...new Map(refs.map(ref => [`${ref.basket_id}:${ref.revision}`, ref])).entries()]
    const pairs = await Promise.all(unique.map(async ([key, ref]) => { try { const [detail, revision] = await Promise.all([api.getBasket(ref.basket_id), api.getBasketRevision(ref.basket_id, ref.revision)]); return [key, { name: detail.name, revision }] as const } catch { return null } }))
    setBaskets(old => ({ ...old, ...Object.fromEntries(pairs.filter(pair => pair !== null)) }))
  }

  const more = async () => {
    if (!cursor || busy) return
    setBusy(true)
    setError('')
    try {
      const page = await api.experiments(cursor)
      const known = { ...strategies }
      const missing = [...new Set(page.items.map(item => item.payload.strategy_revision_id))].filter(id => id && !known[id])
      await Promise.all(missing.map(async id => { try { known[id] = await api.strategy(id) } catch { /* Keep the saved configuration visible without its strategy metadata. */ } }))
      setStrategies(known)
      setDrafts(old => [...old, ...page.items.filter(item => !old.some(existing => existing.id === item.id))])
      setCursor(page.next_cursor)
      await loadBaskets(page.items)
    } catch (reason) { setError(describeError(reason)) } finally { setBusy(false) }
  }

  return <div className="page-stack fixed-strategies-page">
    <header className="page-heading section-heading"><div><h1>固定策略</h1><p>每条配置记录策略、参数与回测条件；相同参数也可以保存为独立配置。</p></div><Link className="button-link" to="/experiments/new">新建固定策略</Link></header>
    {error && <p role="alert" className="error callout">{error} <button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button></p>}
    <section className="panel"><h2>已保存配置</h2><div className="form-grid"><label>资产类型<select value={assetFilter} onChange={event => setAssetFilter(event.target.value)}><option value="">全部</option><option value="etf">ETF</option><option value="stock">股票</option></select></label><label>组合<select value={basketFilter} onChange={event => setBasketFilter(event.target.value)}><option value="">全部</option>{[...new Set(drafts.map(item => item.payload.basket_revision_ref.basket_id))].map(id => <option value={id} key={id}>{Object.values(baskets).find(item => item.revision.basket_id === id)?.name || id}</option>)}</select></label></div>
      {loading && <p>加载固定策略配置…</p>}
      {!loading && !error && drafts.length === 0 && <p className="empty">暂无固定策略配置。新建后可在此查看策略、参数与回测条件。</p>}
      <div className="record-list">{drafts.filter(item => { const ref = item.payload.basket_revision_ref; const members = baskets[`${ref.basket_id}:${ref.revision}`]?.revision.members || []; return (!assetFilter || members.some(member => member.instrument_type === assetFilter)) && (!basketFilter || ref.basket_id === basketFilter) }).map(item => {
        const source = strategies[item.payload.strategy_revision_id]
        const params = item.payload.strategy_params
        const entries = Object.entries(params)
        const spec = item.payload.backtest_spec
        const hasMoreParams = entries.length > 4 || entries.some(([, value]) => describeValue(value).length > 40)
        const ref = item.payload.basket_revision_ref
        const basket = baskets[`${ref.basket_id}:${ref.revision}`]
        const symbols = basket?.revision.members.map(instrumentKey).join(' → ') || ''
        return <article className="record" key={item.id}>
          <div className="record-head"><strong>{source ? String(source.manifest.name || source.manifest.entrypoint) : '已保存策略'}</strong><Link to={`/experiments/${encodeURIComponent(item.id)}`}>编辑配置</Link></div>
          <p className="fixed-params">{entries.length ? entries.slice(0, 4).map(([key, value]) => <span key={key}>{source?.manifest.parameter_schema.properties?.[key]?.title || key}：{compactValue(value)}</span>) : '无策略参数'}</p>
          {hasMoreParams && <details><summary>查看全部参数</summary><dl>{entries.map(([key, value]) => <div key={key}><dt>{source?.manifest.parameter_schema.properties?.[key]?.title || key}</dt><dd>{describeValue(value)}</dd></div>)}</dl></details>}
          <p>{basket?.name || '标的组合'} · <span title={symbols}>{symbols}</span> · {spec.start} — {spec.end}</p>
        </article>
      })}</div>
      {cursor && <button type="button" disabled={busy || loading} onClick={() => void more()}>{busy ? '加载中…' : '加载更多配置'}</button>}
    </section>
  </div>
}
