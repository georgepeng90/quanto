import { expect, test, vi } from 'vitest'
import { api } from '../src/api'

test('history requests preserve pagination anchors and locate without filtering', async () => {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input)
    calls.push(path)
    if (path === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    return Response.json({ items: [], total: 305, page: 1, page_size: 10, total_pages: 31, anchor: 305 })
  }))

  await api.investingModelSummary('m1')
  await api.investingModelHistory('m1', { kind: 'versions', page: 2, page_size: 20, anchor: 305 })
  await api.investingModelHistory('m1', { kind: 'versions', page: 1, page_size: 10, round_number: 53 })
  await api.investingModelHistory('m1', { kind: 'sessions', page: 3, page_size: 50, anchor: 305, session_number: 55 })

  expect(calls).toContain('/api/investing/models/m1?include_history=false')
  expect(calls).toContain('/api/investing/models/m1/history?kind=versions&page=2&page_size=20&anchor=305')
  expect(calls).toContain('/api/investing/models/m1/history?kind=versions&page=1&page_size=10&round_number=53')
  expect(calls).toContain('/api/investing/models/m1/history?kind=sessions&page=3&page_size=50&anchor=305&session_number=55')
})
