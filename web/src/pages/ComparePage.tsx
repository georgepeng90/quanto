import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, describeError } from '../api'
import { RunDifferences } from '../components/RunDifferences'
import { ComparisonCharts } from '../components/ResultCharts'
import type { ComparisonResult, TableRow } from '../types'

export function ComparePage() {
  const [params, setParams] = useSearchParams()
  const [input, setInput] = useState(params.get('run_ids')?.replaceAll(',', '\n') || '')
  const [comparison, setComparison] = useState<ComparisonResult | null>(null)
  const [curves, setCurves] = useState<Record<string, TableRow[]>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])
  const compare = async () => {
    const ids = input.split(/[\s,]+/).map(value => value.trim()).filter(Boolean)
    if (ids.length < 2 || ids.length > 20 || new Set(ids).size !== ids.length) { setError('请输入 2 到 20 个不重复的运行 ID'); setComparison(null); return }
    controller.current?.abort()
    const next = new AbortController()
    controller.current = next
    setBusy(true); setError(''); setComparison(null); setCurves({})
    try {
      const result = await api.comparison(ids, next.signal)
      if (next.signal.aborted) return
      setParams({ run_ids: ids.join(',') })
      setComparison(result)
      if (result.directly_comparable) {
        const loaded = await Promise.all(ids.map(async id => {
          const chart = await api.runChart(id, 'curves', { max_points: 1200 }, next.signal)
          return [id, chart.items] as const
        }))
        if (!next.signal.aborted) setCurves(Object.fromEntries(loaded))
      }
    } catch (value) { if (!next.signal.aborted) setError(describeError(value)) }
    finally { if (!next.signal.aborted) setBusy(false) }
  }
  return <div className="page-stack"><header className="page-heading"><h1>运行对比</h1><p>按输入顺序比较服务端冻结快照与最终指标。</p></header><section className="panel"><label>运行 ID（每行一个）<textarea value={input} onChange={event => setInput(event.target.value)} /></label><div className="inline-actions"><button type="button" disabled={busy} onClick={() => void compare()}>比较运行</button></div>{error && <p className="error callout" role="alert">{error}</p>}</section>{comparison && <RunDifferences comparison={comparison} />}{comparison?.directly_comparable && Object.keys(curves).length > 0 && <ComparisonCharts curves={curves} />}</div>
}
