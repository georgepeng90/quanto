import { expect, test, vi } from 'vitest'
import { api } from '../src/api'

test('learning requests use the authenticated local session, bounded pages and UTC payload', async () => {
  const calls: { path: string; init?: RequestInit }[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    calls.push({ path, init })
    if (path === '/api/session') return Response.json({ token: 'local-token', api_base: '/api' })
    if (path.endsWith('/export')) return new Response('zip-content')
    return Response.json({ items: [], next_cursor: null })
  }))
  const controller = new AbortController()
  await api.learningOverview(controller.signal)
  await api.learningArtifacts('models', 50)
  await api.learningArtifact('backtests', 'id/1')
  await api.learningChart('id/1', controller.signal)
  await api.learningTable('id/1', 'orders', '100', controller.signal)
  await api.jobLogs('job1', '10', controller.signal)
  await api.cancelJob('job1')
  await api.prepareLearning({ spec: { name: '训练集', snapshot_id: 'snapshot', query: { frequency: '1m' }, frequency_minutes: 5 } })
  await api.exportLearning('id/1')
  expect(calls.slice(1).every(call => (call.init?.headers as Record<string, string>).Authorization === 'Bearer local-token')).toBe(true)
  expect(calls.map(call => call.path)).toContain('/api/learning/backtests/id%2F1/charts?max_points=1000')
  expect(calls.map(call => call.path)).toContain('/api/learning/backtests/id%2F1/tables/orders?cursor=100&limit=100')
  expect(calls.map(call => call.path)).toContain('/api/learning/models?cursor=50&limit=50')
  expect(calls.map(call => call.path)).toContain('/api/jobs/job1/logs?cursor=10')
  expect(calls.find(call => call.path === '/api/jobs/job1/cancel')?.init?.method).toBe('POST')
  const prepare = calls.find(call => call.path === '/api/learning/datasets')!
  expect(JSON.parse(prepare.init!.body as string).spec.frequency_minutes).toBe(5)
  expect(calls.find(call => call.path.includes('/charts?'))?.init?.signal).toBe(controller.signal)
})
