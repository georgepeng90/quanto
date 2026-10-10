import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, describeError } from '../api'
import type { BasketDetail, BasketRevisionRef, BasketSummary, FrozenBasketRevision } from '../frameworkTypes'
import { instrumentKey } from '../frameworkTypes'

export function BasketRevisionSelect({ value, onChange, disabled = false }: { value: BasketRevisionRef | null; onChange: (ref: BasketRevisionRef | null) => void; disabled?: boolean }) {
  const [baskets, setBaskets] = useState<BasketSummary[]>([])
  const [detail, setDetail] = useState<BasketDetail | null>(null)
  const [revision, setRevision] = useState<FrozenBasketRevision | null>(null)
  const [error, setError] = useState('')
  const selectionGeneration = useRef(0)
  useEffect(() => () => { selectionGeneration.current += 1 }, [])

  useEffect(() => { let active = true; const load = async () => { try { const items: BasketSummary[] = []; const seen = new Set<string>(); let cursor: string | undefined; do { const page = await api.listBaskets(cursor); items.push(...page.items); cursor = page.next_cursor || undefined; if (cursor) { if (seen.has(cursor)) throw new Error('篮子列表游标未前进。'); seen.add(cursor) } } while (cursor); if (active) setBaskets(items) } catch (reason) { if (active) setError(describeError(reason)) } }; void load(); return () => { active = false } }, [])
  useEffect(() => { let active = true; setDetail(null); if (!value?.basket_id) return; api.getBasket(value.basket_id).then(item => { if (active) { setDetail(item); setError('') } }).catch(reason => { if (active) setError(describeError(reason)) }); return () => { active = false } }, [value?.basket_id])
  useEffect(() => { let active = true; setRevision(null); if (!value) return; api.getBasketRevision(value.basket_id, value.revision).then(item => { if (active) setRevision(item) }).catch(reason => { if (active) setError(describeError(reason)) }); return () => { active = false } }, [value?.basket_id, value?.revision])

  const chooseBasket = async (id: string) => { const generation = ++selectionGeneration.current; if (!id) { setDetail(null); onChange(null); return }; try { const selected = await api.getBasket(id); if (generation !== selectionGeneration.current) return; setDetail(selected); setError(''); onChange({ basket_id: id, revision: selected.current_revision }) } catch (reason) { if (generation === selectionGeneration.current) setError(describeError(reason)) } }
  return <div className="framework-detail">
    <div className="form-grid"><label>标的篮子<select value={value?.basket_id || ''} disabled={disabled} onChange={event => void chooseBasket(event.target.value)}><option value="">请选择篮子</option>{baskets.map(item => <option key={item.basket_id} value={item.basket_id}>{item.name}</option>)}</select></label>
      <label>篮子修订<select value={value?.revision || ''} disabled={disabled || !detail} onChange={event => { if (value) onChange({ basket_id: value.basket_id, revision: Number(event.target.value) }) }}><option value="">请选择修订</option>{detail?.revisions.map(item => <option key={item.revision} value={item.revision}>修订 {item.revision}</option>)}</select></label></div>
    {revision && <p>成员顺序：{revision.members.map(instrumentKey).join(' → ')} · 选择说明：{revision.selection_note || '未填写'}</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {!baskets.length && <p>尚无标的篮子。请先到 <Link to="/datasets?tab=baskets">数据 · 标的篮子</Link> 保存选择。</p>}
  </div>
}
