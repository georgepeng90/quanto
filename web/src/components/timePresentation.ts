const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
})

export function taskTime(value: string | null, missing = '未知'): string {
  if (!value) return missing
  const instant = new Date(value)
  if (!Number.isFinite(instant.getTime())) return missing
  const parts = Object.fromEntries(formatter.formatToParts(instant).map(part => [part.type, part.value]))
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`
}
