import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { CurrentModelSnapshot } from '../src/components/CurrentModelSnapshot'
import type { InvestingModelSummary } from '../src/investingTypes'

const model = { id: 'm1', version_count: 305, latest_version_id: 'v305', spec: { validation_start: '2025-01-01', validation_end: '2025-12-31', selection_metric: 'validation_sharpe' } } as InvestingModelSummary
const latest = { id: 'v305', round_number: 305, best_round_number: 244, best_source_version_id: 'v244', selection_valid: true, validation_best: { total_return: .3958, max_drawdown: .0635, sharpe: 2.1527 }, validation_latest: { total_return: .4532, max_drawdown: .0996, sharpe: 1.9175 } }
const view = (value = model) => <MemoryRouter><CurrentModelSnapshot model={value} /></MemoryRouter>
beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://local')
    if (url.pathname === '/api/session') return Response.json({ token: 'token', api_base: '/api' })
    if (url.pathname.endsWith('/history') && url.searchParams.get('kind') === 'versions' && url.searchParams.get('page_size') === '1' && url.searchParams.get('round_number') === '305') return Response.json({ items: [latest] })
    throw new Error(`Unexpected request ${url}`)
  }))
})

test('current snapshot shows retained best performance and a pinned backtest, separately from continuation', async () => {
  render(view())
  const card = await screen.findByRole('region', { name: '当前选定快照' })
  expect(await within(card).findByText('第244轮快照')).toBeVisible()
  expect(within(card).getByRole('definition', { name: '总收益率' })).toHaveTextContent('39.58%')
  expect(within(card).getByRole('definition', { name: '最大回撤（日末）' })).toHaveTextContent('6.35%')
  expect(within(card).getByRole('definition', { name: '夏普比率（日末）' })).toHaveTextContent('2.1527')
  expect(within(card).queryByText('45.32%')).toBeNull()
  expect(within(card).getByText(/最新保存：第305轮/)).toBeVisible()
  expect(within(card).getByRole('link', { name: '使用当前快照回测' })).toHaveAttribute('href', '/runs?model=m1&version=v244')
})

test('current snapshot clears a previous version on update and supports retry after a failed read', async () => {
  const rendered = render(view())
  await screen.findByText('第244轮快照')
  let failures = 0
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).includes('round_number=306')) {
      if (!failures++) return Response.json({ message: 'Snapshot temporarily unavailable' }, { status: 503 })
      return Response.json({ items: [{ ...latest, id: 'v306', round_number: 306, best_round_number: 306, best_source_version_id: 'v306', validation_best: { total_return: .5 } }] })
    }
    return prior(input, init)
  }))
  rendered.rerender(view({ ...model, version_count: 306, latest_version_id: 'v306' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Snapshot temporarily unavailable')
  expect(screen.queryByText('第244轮快照')).toBeNull()
  expect(screen.queryByRole('link', { name: '使用当前快照回测' })).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: '重试快照' }))
  expect(await screen.findByText('第306轮快照')).toBeVisible()
  expect(screen.getByRole('link', { name: '使用当前快照回测' })).toHaveAttribute('href', '/runs?model=m1&version=v306')
})

test('unscored snapshots disclose that their fallback is not a validated optimum', async () => {
  const prior = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input).includes('/history?')
    ? Response.json({ items: [{ ...latest, selection_valid: false, best_round_number: 305, validation_best: {} }] }) : prior(input, init)))
  render(view())
  expect(await screen.findByText(/尚无有效验证评分/)).toBeVisible()
  expect(screen.queryByText('第305轮快照')).toBeNull()
  expect(screen.getByRole('definition', { name: '总收益率' })).toHaveTextContent('—')
  expect(screen.queryByRole('link', { name: '使用当前快照回测' })).toBeNull()
})

test('untrained models have no current snapshot or snapshot request', async () => {
  render(view({ ...model, latest_version_id: null, version_count: 0 }))
  await waitFor(() => expect(screen.getByText('尚未训练，暂无可用快照。')).toBeVisible())
  expect(screen.queryByRole('link', { name: '使用当前快照回测' })).toBeNull()
  expect(globalThis.fetch).not.toHaveBeenCalled()
})
