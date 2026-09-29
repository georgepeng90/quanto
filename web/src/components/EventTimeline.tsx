import type { RunEvent } from '../types'
import { phaseLabel } from './RunProgress'

export function EventTimeline({ events }: { events: RunEvent[] }) {
  return <ol className="event-timeline">{events.map(event => <li key={event.seq}>
    <strong>{event.asof || '任务事件'} · {phaseLabel(event.phase, event.code)}</strong> <span className="muted">#{event.seq} · {event.code}</span>
    <p>{event.message}</p>
  </li>)}</ol>
}
