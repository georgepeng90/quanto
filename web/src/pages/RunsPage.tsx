import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { BacktestCatalog } from '../components/BacktestCatalog'
import { ModelBacktestForm } from '../components/ModelBacktestForm'
import { FixedBacktestForm } from '../components/FixedBacktestForm'
import { api, describeError } from '../api'
import { terminalStates } from '../components/RunProgress'
import type { BacktestCatalogRow } from '../types'

export function RunsPage() {
  const [searchParams] = useSearchParams()
  const initialModel = searchParams.get('model') || ''
  const initialVersion = searchParams.get('version') || ''
  const [creating, setCreating] = useState(!!initialModel)
  const [source, setSource] = useState(initialModel ? 'model' : 'fixed')
  useEffect(() => { if (initialModel) { setCreating(true); setSource('model') } }, [initialModel, initialVersion])
  const [catalog, setCatalog] = useState<BacktestCatalogRow[]>([])
  const [listComplete, setListComplete] = useState(false)
  const [listBusy, setListBusy] = useState(false)
  const [error, setError] = useState('')
  const loadGeneration = useRef(0)
  const mountController = useRef<AbortController | null>(null)

  const loadCatalog = async (signal?: AbortSignal) => {
    const generation = ++loadGeneration.current
    setListBusy(true)
    let cursor: string | undefined
    const rows: BacktestCatalogRow[] = []
    const seen = new Set<string>()
    try {
      for (;;) {
        const page = await api.backtestCatalog(cursor, signal)
        if (signal?.aborted || generation !== loadGeneration.current) return
        for (const row of page.items) if (!seen.has(row.id)) { rows.push(row); seen.add(row.id) }
        if (!page.next_cursor) break
        if (page.next_cursor === cursor) throw new Error('Catalog cursor did not advance')
        cursor = page.next_cursor
      }
      if (signal?.aborted || generation !== loadGeneration.current) return
      setCatalog(rows)
      setListComplete(true)
      setError('')
    } catch (value) { if (!signal?.aborted && generation === loadGeneration.current) setError(describeError(value)) }
    finally { if (!signal?.aborted && generation === loadGeneration.current) setListBusy(false) }
  }

  useEffect(() => {
    const controller = new AbortController()
    mountController.current = controller
    void loadCatalog(controller.signal)
    return () => { controller.abort(); mountController.current = null }
  }, [])

  useEffect(() => {
    if (!listComplete || !catalog.some(row => !terminalStates.has(row.state))) return
    const timer = window.setInterval(() => { void loadCatalog(mountController.current?.signal) }, 3000)
    return () => clearInterval(timer)
  }, [catalog, listComplete])

  return <div className="page-stack runs-page"><header className="page-heading section-heading"><div><h1>回测</h1><p>查看全部历史报告、比较冻结条件，或使用固定策略与模型保存版本发起回测。</p></div><button onClick={() => setCreating(value => !value)} aria-expanded={creating}>{creating ? '收起新建回测' : '新建回测'}</button></header>
    {error && <p role="alert" className="error callout">{error}</p>}
    <BacktestCatalog rows={catalog} complete={listComplete} busy={listBusy} refresh={() => void loadCatalog(mountController.current?.signal)} />
    <Link to="/compare">固定策略曲线对比</Link>
    {creating && <><section className="panel"><h2>新建回测</h2><label>回测来源<select value={source} onChange={event => setSource(event.target.value)}><option value="fixed">固定策略</option><option value="model">强化学习保存版本</option></select></label></section>
    {source === 'model' ? <ModelBacktestForm key={`${initialModel}:${initialVersion}`} initialModel={initialModel} initialVersion={initialVersion} onSubmitted={() => void loadCatalog(mountController.current?.signal)} /> : <FixedBacktestForm onSubmitted={() => void loadCatalog(mountController.current?.signal)} />
    }</>}
  </div>
}
