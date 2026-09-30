import { afterEach, expect, test, vi } from 'vitest'
import { api } from '../src/api'

afterEach(() => vi.useRealTimers())

const unavailable = () => Response.json({ message: '暂时不可用' }, { status: 503 })
const mockServer = (respond: () => Response) => {
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async input => {
    if (String(input) === '/api/session') return Response.json({ token: 'secret', api_base: '/api', timezone: 'Asia/Shanghai' })
    requests.push(String(input))
    return respond()
  }))
  return requests
}

test('stops a temporary result failure after three attempts', async () => {
  vi.useFakeTimers()
  const requests = mockServer(unavailable)
  const failed = expect(api.runChart('r1', 'curves')).rejects.toMatchObject({ status: 503 })
  await vi.advanceTimersByTimeAsync(1000)
  await failed
  await vi.advanceTimersByTimeAsync(10000)
  expect(requests).toHaveLength(3)
})

test('cancelling a view also cancels its pending retry', async () => {
  vi.useFakeTimers()
  const requests = mockServer(unavailable)
  const controller = new AbortController()
  const aborted = expect(api.runTable('r1', 'equity', {}, controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  await vi.advanceTimersByTimeAsync(0)
  expect(requests).toHaveLength(1)
  controller.abort()
  await aborted
  await vi.advanceTimersByTimeAsync(10000)
  expect(requests).toHaveLength(1)
})

test('does not retry a permanent validation error', async () => {
  const requests = mockServer(() => Response.json({ message: '产物校验失败' }, { status: 422 }))
  await expect(api.runTable('r1', 'orders')).rejects.toMatchObject({ status: 422 })
  expect(requests).toHaveLength(1)
})
