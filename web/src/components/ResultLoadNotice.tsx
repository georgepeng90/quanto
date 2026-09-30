import type { TableLoadState } from '../types'

export function ResultLoadNotice({ state, label, onRetry }: { state?: TableLoadState; label: string; onRetry?: () => void }) {
  if (state?.loading) return <p role="status" className="callout">正在加载{label}…</p>
  if (state?.error) return <div role="alert" className="error callout">{label}加载失败：{state.error}。{onRetry && <button type="button" onClick={onRetry}>重试{label}</button>}</div>
  return null
}
