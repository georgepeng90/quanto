import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, describeError } from '../api'
import type { BasketRevisionRef, BasketSummary, FrozenBasketRevision } from '../frameworkTypes'
import { instrumentKey } from '../frameworkTypes'

export function BasketRevisionSelect({ value, onChange, disabled = false }: { value: BasketRevisionRef | null; onChange: (ref: BasketRevisionRef | null) => void; disabled?: boolean }) {
  const [baskets, setBaskets] = useState<BasketSummary[]>([])
  const [revision, setRevision] = useState<FrozenBasketRevision | null>(null)
  const [error, setError] = useState('')
  const selectionGeneration = useRef(0)
  useEffect(() => () => { selectionGeneration.current += 1 }, [])

  useEffect(() => { let active = true; const load = async () => { try { const items: BasketSummary[] = []; const seen = new Set<string>(); let cursor: string | undefined; do { const page = await api.listBaskets(cursor); items.push(...page.items); cursor = page.next_cursor || undefined; if (cursor) { if (seen.has(cursor)) throw new Error('篮子列表游标未前进。'); seen.add(cursor) } } while (cursor); if (active) setBaskets(items) } catch (reason) { if (active) setError(describeError(reason)) } }; void load(); return () => { active = false } }, [])
  useEffect(() => { let active = true; setRevision(null); if (!value) return; api.getBasketRevision(value.basket_id, value.revision).then(item => { if (active) setRevision(item) }).catch(reason => { if (active) setError(describeError(reason)) }); return () => { active = false } }, [value?.basket_id, value?.revision])

  const chooseBasket = async (id: string) => { const generation = ++selectionGeneration.current; if (!id) { onChange(null); return }; try { const selected = await api.getBasket(id); if (generation !== selectionGeneration.current) return; setError(''); onChange({ basket_id: id, revision: selected.current_revision }) } catch (reason) { if (generation === selectionGeneration.current) setError(describeError(reason)) } }
  return <div className="framework-detail">
    <label>标的组合<select value={value?.basket_id || ''} disabled={disabled} onChange={event => void chooseBasket(event.target.value)}><option value="">请选择组合</option>{baskets.map(item => <option key={item.basket_id} value={item.basket_id}>{item.name}</option>)}</select></label>
    {revision && <p>组合成员：{revision.members.map(instrumentKey).join(' → ')}</p>}
    {error && <p role="alert" className="error">{error}</p>}
    {!baskets.length && <p>尚无标的篮子。请先到 <Link to="/datasets?tab=baskets">数据 · 标的篮子</Link> 保存选择。</p>}
  </div>
}
