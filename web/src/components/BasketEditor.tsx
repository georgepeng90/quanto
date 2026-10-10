import { useEffect, useRef, useState } from 'react'
import { api, describeError } from '../api'
import type { BasketDetail, BasketSummary, Definition, TradableInstrumentId } from '../frameworkTypes'
import { instrumentKey } from '../frameworkTypes'

const blank = (): TradableInstrumentId => ({ exchange: 'SSE', instrument_type: 'etf', code: '' })

export function BasketEditor() {
  const [baskets, setBaskets] = useState<BasketSummary[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [definitions, setDefinitions] = useState<Definition[]>([])
  const [detail, setDetail] = useState<BasketDetail | null>(null)
  const [history, setHistory] = useState<BasketDetail['current'] | null>(null)
  const [name, setName] = useState('')
  const [members, setMembers] = useState<TradableInstrumentId[]>([blank()])
  const [selectionNote, setSelectionNote] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const createAttempt = useRef<{ body: string; key: string } | null>(null)

  const refresh = async () => { const page = await api.listBaskets(); setBaskets(page.items); setCursor(page.next_cursor) }
  useEffect(() => { let alive = true; Promise.all([api.listBaskets(), api.listDefinitions()]).then(([page, installed]) => { if (alive) { setBaskets(page.items); setCursor(page.next_cursor); setDefinitions(installed.items) } }).catch(value => { if (alive) setError(describeError(value)) }); return () => { alive = false } }, [])
  const edit = (index: number, update: Partial<TradableInstrumentId>) => setMembers(old => old.map((member, position) => position === index ? { ...member, ...update } : member))
  const move = (index: number, offset: number) => setMembers(old => { const next = [...old]; const target = index + offset; if (target < 0 || target >= next.length) return old; [next[index], next[target]] = [next[target], next[index]]; return next })
  const select = async (id: string) => { setError(''); try { const selected = await api.getBasket(id); setDetail(selected); setHistory(null); setName(selected.name); setMembers(selected.current.members); setSelectionNote(selected.current.selection_note) } catch (value) { setError(describeError(value)) } }
  const reset = () => { setDetail(null); setHistory(null); setName(''); setMembers([blank()]); setSelectionNote(''); setError('') }
  const save = async () => {
    const normalized = members.map(member => ({ exchange: member.exchange.trim().toUpperCase(), instrument_type: member.instrument_type, code: member.code.trim() }))
    if (!name.trim() || normalized.some(member => !/^[A-Z0-9_]+$/.test(member.exchange) || !/^[A-Za-z0-9_.-]+$/.test(member.code)) || new Set(normalized.map(instrumentKey)).size !== normalized.length) { setError('请输入名称及不重复的交易所、类型和代码。'); return }
    setSaving(true); setError('')
    try {
      if (detail) {
        const revision = await api.reviseBasket(detail.basket_id, { expected_revision: detail.current_revision, members: normalized, selection_note: selectionNote })
        setDetail({ ...detail, current_revision: revision.revision, current: revision, revisions: [...detail.revisions, { revision: revision.revision, content_hash: revision.content_hash, created_at: revision.created_at }] })
      } else {
        const requestBody = { name: name.trim(), members: normalized, selection_note: selectionNote }
        const body = JSON.stringify(requestBody)
        if (createAttempt.current?.body !== body) createAttempt.current = { body, key: crypto.randomUUID() }
        const revision = await api.createBasket({ ...requestBody, idempotency_key: createAttempt.current.key })
        setDetail({ basket_id: revision.basket_id, name: requestBody.name, current_revision: revision.revision, created_at: revision.created_at, current: revision, revisions: [{ revision: revision.revision, content_hash: revision.content_hash, created_at: revision.created_at }] })
        setBaskets(old => old.some(item => item.basket_id === revision.basket_id) ? old : [...old, { basket_id: revision.basket_id, name: requestBody.name, current_revision: revision.revision, created_at: revision.created_at }])
        createAttempt.current = null
        try { await refresh(); setDetail(await api.getBasket(revision.basket_id)) }
        catch (value) { setError(`篮子已保存，但刷新失败：${describeError(value)}`) }
      }
    } catch (value) { setError(describeError(value)) }
    finally { setSaving(false) }
  }

  return <div className="page-stack framework-detail">
    {error && <p role="alert" className="error callout">{error}</p>}
    <section className="panel"><div className="section-heading"><div><h2>已保存篮子</h2><p>篮子保存选择意图；数据与运行能力按具体配置由服务端检查。</p></div><button type="button" onClick={reset}>新建篮子</button></div>
      {baskets.length ? <div className="record-list">{baskets.map(basket => <button type="button" key={basket.basket_id} className="record" onClick={() => select(basket.basket_id)}>{basket.name} · 修订 {basket.current_revision}</button>)}</div> : <p className="empty">尚无篮子。</p>}
      {cursor && <button type="button" onClick={async () => { try { const page = await api.listBaskets(cursor); setBaskets(old => [...old, ...page.items]); setCursor(page.next_cursor) } catch (value) { setError(describeError(value)) } }}>加载更多篮子</button>}
    </section>
    <section className="panel"><h2>{detail ? `编辑 ${detail.name} · 当前修订 ${detail.current_revision}` : '新建标的篮子'}</h2><p className="muted">按观察和动作所需顺序排列。手动录入的标的身份仍待数据证据核验。</p>
      <label>篮子名称<input value={name} onChange={event => setName(event.target.value)} disabled={Boolean(detail)} /></label>
      <div className="record-list">{members.map((member, index) => <div className="record" key={index}>
        <div className="form-grid"><label>交易所<input value={member.exchange} onChange={event => edit(index, { exchange: event.target.value })} placeholder="SSE" /></label><label>类型<select value={member.instrument_type} onChange={event => edit(index, { instrument_type: event.target.value as TradableInstrumentId['instrument_type'] })}><option value="etf">ETF</option><option value="stock">股票</option></select></label><label>代码<input value={member.code} onChange={event => edit(index, { code: event.target.value })} placeholder="512480" /></label></div>
        <p className="muted">规范身份：{instrumentKey(member)}</p><div className="inline-actions"><button type="button" aria-label={`上移成员 ${index + 1}`} disabled={index === 0} onClick={() => move(index, -1)}>上移</button><button type="button" aria-label={`下移成员 ${index + 1}`} disabled={index === members.length - 1} onClick={() => move(index, 1)}>下移</button><button type="button" aria-label={`移除成员 ${index + 1}`} disabled={members.length === 1} onClick={() => setMembers(old => old.filter((_, position) => position !== index))}>移除</button></div>
      </div>)}</div>
      <button type="button" onClick={() => setMembers(old => [...old, blank()])}>添加成员</button>
      <label>选择说明<textarea value={selectionNote} onChange={event => setSelectionNote(event.target.value)} /></label>
      <button type="button" disabled={saving} onClick={save}>{saving ? '保存中…' : detail ? '创建新修订' : '保存篮子'}</button>
      {detail && <p className="success">已保存修订 {detail.current.revision} · {detail.current.members.map(instrumentKey).join(' → ')}</p>}
      {detail && <div><h3>历史修订</h3><div className="inline-actions">{detail.revisions.map(item => <button type="button" key={item.revision} onClick={async () => { try { setHistory(await api.getBasketRevision(detail.basket_id, item.revision)) } catch (value) { setError(describeError(value)) } }}>查看修订 {item.revision}</button>)}</div>{history && <p>修订 {history.revision} · {history.members.map(instrumentKey).join(' → ')} · {history.selection_note}</p>}</div>}
      {members.some(member => member.instrument_type === 'stock') && !definitions.some(definition => definition.ref.kind !== 'feature' && definition.supported_instrument_types.includes('stock')) && <p className="muted">已安装定义中没有股票运行方案；篮子仍可保存。运行前需检查具体配置的服务端能力报告。</p>}
      {new Set(members.map(member => member.instrument_type)).size > 1 && <p className="muted">混合篮子运行需通过服务端能力检查，当前不提供直接运行入口。</p>}
    </section>
  </div>
}
