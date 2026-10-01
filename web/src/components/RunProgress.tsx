import { useEffect, useState } from 'react'
import type { RunEvent, RunRecord, RunState } from '../types'
import { taskTime } from './timePresentation'

export const terminalStates = new Set<RunState>(['succeeded', 'failed', 'cancelled', 'interrupted'])
export const stateLabel: Record<RunState, string> = { queued: '排队中', running: '运行中', cancel_requested: '正在取消', succeeded: '已完成', failed: '失败', cancelled: '已取消', interrupted: '已中断' }
export const phaseLabel = (phase: string, code?: string) => code === 'WORKER_COMPLETED' ? '任务完成' : ({ worker: '任务准备', backtest: '回测执行' })[phase as 'worker' | 'backtest'] || phase

function duration(start: string | null, end: string | null, now: number) {
  if (!start) return '未知'
  const elapsed = Math.max(0, Math.floor(((end ? Date.parse(end) : now) - Date.parse(start)) / 1000))
  if (!Number.isFinite(elapsed)) return '未知'
  return `${Math.floor(elapsed / 3600)}时 ${Math.floor(elapsed % 3600 / 60)}分 ${elapsed % 60}秒`
}

export function RunProgress({ run, latest, progress }: { run: RunRecord; latest?: RunEvent; progress?: RunEvent }) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (terminalStates.has(run.state)) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [run.state])
  const knownTotal = progress && progress.total_days > 0
  return <div className="run-progress">
    <p><span className="badge">{stateLabel[run.state]}</span> · 阶段：{latest?.phase ? phaseLabel(latest.phase, latest.code) : (run.state === 'queued' ? '等待调度' : '暂无阶段事件')}</p>
    {knownTotal ? <p>已完成交易日 {progress.completed_days} / {progress.total_days}</p> : <p>已完成交易日：{latest?.completed_days ?? 0} · 总量待服务端确认</p>}
    {knownTotal && <progress value={progress.completed_days} max={progress.total_days} aria-label="交易日进度" />}
    <p>模拟日期：{latest?.asof || progress?.asof || run.completed_through || '尚未开始'} · 已完成至：{run.completed_through || '尚无完整交易日'}</p>
    <p className="muted">任务时间：北京时间（Asia/Shanghai，UTC+8）。模拟日期为回测交易日。</p>
    <p>提交：{taskTime(run.created_at, '历史记录未知')} · 开始：{taskTime(run.started_at, '尚未开始或历史记录未知')} · 结束：{taskTime(run.finished_at, '尚未结束或历史记录未知')}</p>
    <p>运行耗时：{duration(run.started_at, run.finished_at, now)}</p>
  </div>
}
