import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, describeError } from '../api'
import { taskTime } from '../components/timePresentation'
import type { ScreeningSummary } from '../types'

export function ScreeningsPage() {
  const [items, setItems] = useState<ScreeningSummary[]>([]), [cursor, setCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true), [error, setError] = useState(''), [refresh, setRefresh] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError('')
    api.screenings(undefined, controller.signal).then(page => { if (!controller.signal.aborted) { setItems(page.items); setCursor(page.next_cursor) } })
      .catch(value => { if (!controller.signal.aborted) setError(describeError(value)) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [refresh])
  const more = async () => {
    if (!cursor) return
    setLoading(true)
    try { const page = await api.screenings(cursor); setItems(old => [...old, ...page.items]); setCursor(page.next_cursor); setError('') }
    catch (value) { setError(describeError(value)) } finally { setLoading(false) }
  }
  return <div className="page-stack"><header className="page-heading section-heading"><div><h1>个股筛选</h1><p>查看 Agent 提交的公司研究、规则检查和证据，再交给量价模型使用。</p></div><button disabled={loading} onClick={() => setRefresh(value => value + 1)}>刷新结果</button></header>
    {error && <p role="alert" className="error callout">{error}</p>}{loading && <p>读取筛选记录…</p>}
    {!loading && !error && !items.length && <section className="panel"><p>尚无筛选结果。请通过 Agent API 或 CLI 提交公司研究及来源材料。</p></section>}
    <section className="panel record-list">{items.map(item => <article className="record" key={item.id}><div className="record-head"><Link to={`/screenings/${encodeURIComponent(item.id)}`}>{item.name}</Link><span className="badge">{item.selected_count ? '研究已保存' : '无入选股票'}</span></div><p>{taskTime(item.as_of)} · 入选 {item.selected_count} · 研究范围 {item.candidate_count}</p>{item.shortfall > 0 && <p className="muted">比目标少 {item.shortfall} 只，未放宽条件。</p>}</article>)}{cursor && <button disabled={loading} onClick={() => void more()}>加载更多结果</button>}</section>
  </div>
}
