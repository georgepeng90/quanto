import { useEffect, useState } from 'react'
import type { ParameterSchema } from '../types'

type Props = { schema: ParameterSchema; value: Record<string, unknown>; onChange: (value: Record<string, unknown>) => void; onValidityChange?: (valid: boolean) => void }

function JsonParameter({ label, id, value, onChange, onValidityChange }: { label: string; id: string; value: unknown; onChange: (value: unknown) => void; onValidityChange: (valid: boolean) => void }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2))
  const [error, setError] = useState(false)
  useEffect(() => { setText(JSON.stringify(value, null, 2)); setError(false); onValidityChange(true) }, [value])
  return <label htmlFor={id}>{label} · JSON<textarea id={id} spellCheck={false} value={text} onChange={event => { setText(event.target.value); try { onChange(JSON.parse(event.target.value)); setError(false); onValidityChange(true) } catch { setError(true); onValidityChange(false) } }} />{error && <small className="error">JSON 格式未完成，不能保存。</small>}</label>
}

export function ParameterForm({ schema, value, onChange, onValidityChange }: Props) {
  const [text, setText] = useState(JSON.stringify(value, null, 2))
  const [error, setError] = useState('')
  const [complexValidity, setComplexValidity] = useState<Record<string, boolean>>({})
  useEffect(() => { setText(JSON.stringify(value, null, 2)); setError('') }, [value])
  useEffect(() => { onValidityChange?.(!error && Object.values(complexValidity).every(Boolean)) }, [error, complexValidity, onValidityChange])
  const fields = schema.properties || {}
  const setField = (key: string, next: unknown) => onChange({ ...value, [key]: next })

  return <div className="parameter-form">
    {Object.entries(fields).map(([key, field]) => {
      const label = field.title || key
      const current = Object.prototype.hasOwnProperty.call(value, key) ? value[key] ?? '' : ''
      const id = `param-${key}`
      if (field.enum) return <label key={key} htmlFor={id}>{label}<select id={id} value={String(current)} onChange={event => setField(key, event.target.value || undefined)}><option value="">{field.default === undefined ? '请选择' : `按 schema 默认值：${String(field.default)}`}</option>{field.enum.map(choice => <option key={String(choice)} value={String(choice)}>{String(choice)}</option>)}</select></label>
      if (field.type === 'boolean') return <label key={key} htmlFor={id}>{label}<select id={id} value={current === '' ? '' : String(current)} onChange={event => setField(key, event.target.value === '' ? undefined : event.target.value === 'true')}><option value="">{field.default === undefined ? '请选择' : `按 schema 默认值：${String(field.default)}`}</option><option value="true">是</option><option value="false">否</option></select></label>
      if (field.type === 'integer' || field.type === 'number') return <label key={key} htmlFor={id}>{label}<input id={id} type="number" step={field.type === 'integer' ? '1' : 'any'} min={field.minimum} max={field.maximum} placeholder={field.default === undefined ? undefined : `默认 ${String(field.default)}`} value={String(current)} onChange={event => setField(key, event.target.value === '' ? undefined : Number(event.target.value))} /></label>
      if (field.type === 'string') return <label key={key} htmlFor={id}>{label}<input id={id} placeholder={field.default === undefined ? undefined : `默认 ${String(field.default)}`} value={String(current)} onChange={event => setField(key, event.target.value)} /></label>
      return <JsonParameter key={key} id={id} label={label} value={current} onChange={next => setField(key, next)} onValidityChange={valid => setComplexValidity(old => old[key] === valid ? old : { ...old, [key]: valid })} />
    })}
    {Object.keys(fields).length === 0 && <p className="muted">此策略没有可编辑参数。</p>}
    {Object.values(fields).some(field => field.default !== undefined) && <p className="muted">留空字段由服务端按 schema 默认值解析；保存后可查看有效参数。</p>}
    <label htmlFor="parameter-json">参数 JSON（与表单共享状态）</label>
    <textarea id="parameter-json" spellCheck={false} value={text} onChange={event => {
      setText(event.target.value)
      try { const parsed = JSON.parse(event.target.value); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error(); onChange(parsed); setError('') } catch { setError('参数 JSON 必须是对象，不能保存。') }
    }} />
    {error && <p role="alert" className="error">{error}</p>}
  </div>
}
