import { useEffect, useRef, useState } from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { python } from '@codemirror/lang-python'
import { json } from '@codemirror/lang-json'
import { api, describeError } from '../api'
import type { StrategyManifest, StrategyRevision } from '../types'

const initialManifest: StrategyManifest = { entrypoint: 'strategy.py:create_strategy', parameter_schema: { type: 'object', properties: {} }, auxiliary_sources: [], research_data: [] }
const initialFiles = { 'strategy.json': JSON.stringify(initialManifest, null, 2), 'strategy.py': '' }

export function StrategyEditor({ revision, onSaved, onDirtyChange }: { revision: StrategyRevision | null; onSaved: (value: StrategyRevision) => void; onDirtyChange: (dirty: boolean) => void }) {
  const generation = useRef(0)
  const identity = useRef(0)
  const [saving, setSaving] = useState(false)
  const [files, setFiles] = useState<Record<string, string>>(initialFiles)
  const [active, setActive] = useState('strategy.py')
  const [dirty, setDirty] = useState(false)
  const [message, setMessage] = useState('')
  const [newName, setNewName] = useState('')
  useEffect(() => { identity.current += 1; generation.current += 1; setFiles(revision?.files || initialFiles); setActive(revision?.manifest.entrypoint.split(':')[0] || 'strategy.py'); setDirty(false); onDirtyChange(false); setMessage(''); return () => { identity.current += 1 } }, [revision?.id])
  const change = (name: string, content: string) => { generation.current += 1; setFiles(old => ({ ...old, [name]: content })); setDirty(true); onDirtyChange(true) }
  const add = () => {
    generation.current += 1
    const name = newName.trim()
    if (!name || files[name] || !/^(?!\/)(?!.*(?:^|\/)\.\.\/)[\w./-]+\.py$/.test(name)) { setMessage('请输入未使用的相对 .py 文件名'); return }
    try {
      const manifest = JSON.parse(files['strategy.json']) as StrategyManifest
      if (!Array.isArray(manifest.auxiliary_sources)) throw new Error()
      if (name !== manifest.entrypoint.split(':')[0]) manifest.auxiliary_sources = [...new Set([...manifest.auxiliary_sources, name])]
      setFiles(old => ({ ...old, [name]: '', 'strategy.json': JSON.stringify(manifest, null, 2) })); setActive(name); setNewName(''); setDirty(true); onDirtyChange(true)
    } catch { setMessage('请先修正 strategy.json，再添加文件。') }
  }
  const remove = () => {
    generation.current += 1
    if (active === 'strategy.json') return
    try {
      const manifest = JSON.parse(files['strategy.json']) as StrategyManifest
      if (active === manifest.entrypoint.split(':')[0]) { setMessage('入口源码不可移除；请先修改入口声明。'); return }
      manifest.auxiliary_sources = manifest.auxiliary_sources.filter(name => name !== active)
      const next: Record<string, string> = { ...files, 'strategy.json': JSON.stringify(manifest, null, 2) }; delete next[active]; setFiles(next); setActive('strategy.json'); setDirty(true); onDirtyChange(true)
    } catch { setMessage('请先修正 strategy.json，再移除文件。') }
  }
  const save = async () => {
    if (saving) return
    const submittedIdentity = identity.current, submittedGeneration = generation.current
    setSaving(true)
    try {
      const manifest = JSON.parse(files['strategy.json']) as StrategyManifest
      if (!manifest || manifest.parameter_schema?.type !== 'object' || !Array.isArray(manifest.auxiliary_sources) || !Array.isArray(manifest.research_data)) throw new Error('strategy.json 声明不完整')
      if (!files[manifest.entrypoint.split(':')[0]]) throw new Error('入口源码文件为空或不存在')
      const declared = new Set(['strategy.json', manifest.entrypoint.split(':')[0], ...manifest.auxiliary_sources])
      const undeclared = Object.keys(files).filter(name => !declared.has(name))
      if (undeclared.length) throw new Error(`源码未列入 auxiliary_sources：${undeclared.join('、')}`)
      const saved = await api.registerStrategy(manifest, files)
      if (submittedIdentity !== identity.current) return
      if (submittedGeneration !== generation.current) { setMessage('提交时的策略源码已保存；后续修改仍未保存。'); return }
      setDirty(false); onDirtyChange(false); setMessage('策略源码已保存'); onSaved({ ...saved, files })
    } catch (error) { if (submittedIdentity === identity.current) setMessage(describeError(error)) }
    finally { setSaving(false) }
  }
  return <section className="panel strategy-editor">
    <div className="section-heading"><div><h2>策略源码</h2><p>服务端先检查源码，运行验证需另行执行。</p></div><button type="button" onClick={save} disabled={saving}>保存策略源码</button></div>
    <div className="editor-layout"><div className="file-list" aria-label="源码文件">{Object.keys(files).sort().map(name => <button type="button" className={active === name ? 'selected' : ''} key={name} onClick={() => setActive(name)}>{name}</button>)}
      <label htmlFor="new-source">新增源码文件</label><input id="new-source" value={newName} onChange={event => setNewName(event.target.value)} placeholder="helper.py" /><button type="button" onClick={add}>添加文件</button><button type="button" onClick={remove} disabled={active === 'strategy.json'}>移除当前文件</button></div>
      <div className="source-pane"><div className="source-heading"><strong>{active}</strong>{dirty && <span className="badge warn">未保存</span>}</div><CodeMirror key={active} aria-label={`${active} 源码`} value={files[active] || ''} height="310px" extensions={[active.endsWith('.json') ? json() : python()]} onChange={value => change(active, value)} /></div></div>
    {message && <p role="status" className={message.startsWith('已注册') ? 'success' : 'error'}>{message}</p>}
  </section>
}
