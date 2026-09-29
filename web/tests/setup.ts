import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

afterEach(cleanup)
// jsdom's AbortSignal and Node's undici Request use different realms. Data-router
// navigation only needs request metadata in these tests; API fetch is mocked below.
vi.stubGlobal('Request', class { url: string; method: string; signal: AbortSignal | undefined; headers: Headers; constructor(url: string, init: RequestInit = {}) { this.url = url; this.method = init.method || 'GET'; this.signal = init.signal || undefined; this.headers = new Headers(init.headers) } })

Range.prototype.getClientRects = () => ({ length: 0, item: () => null, [Symbol.iterator]: function* () {} }) as DOMRectList
Range.prototype.getBoundingClientRect = () => new DOMRect(0, 0, 0, 0)
