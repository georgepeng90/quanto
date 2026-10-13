import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { expect, test, vi } from 'vitest'
import { ScreeningsPage } from '../src/pages/ScreeningsPage'
import { ScreeningDetailPage } from '../src/pages/ScreeningDetailPage'

const identity = { exchange: 'SZSE', instrument_type: 'stock', code: '000001' }
const record = (selected: boolean) => ({ id: 'r1', request: { name: '银行研究', as_of: '2026-10-13T08:00:00Z', purpose: '公司资讯初筛',
  universe_description: '人工提供名单', universe_kind: 'provided_list', agent_model: 'test', prompt_version: '1',
  rules: { target_count: 2 }, candidates: [{ instrument_id: identity, name: '示例银行', business_summary: '银行服务', reasons: ['盈利稳定'], risks: ['信用风险'], counter_evidence: [], unknowns: ['尚待核实资本充足率'], citations: { name: [{ evidence_id: 'e1', quote: '银行' }] } }],
  evidence: [{ id: 'e1', source_name: '公司公告', source_locator: 'https://example.org/report', text: '银行', published_at: '2026-10-12', retrieved_at: '2026-10-13T07:00:00Z', archive_level: 'excerpt' }] },
  result: { members: selected ? [identity] : [], candidates: [{ instrument_id: identity, decision: selected ? 'selected' : 'excluded', reasons: selected ? [] : ['missing_status'] }], shortfall: selected ? 1 : 2 } })

test.each([true, false])('detail explains evidence and preserves handoff (selected=%s)', async selected => {
  const posts: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 't', api_base: '/api' })
    if (init?.method === 'POST') { posts.push(path); return Response.json({ basket_id: 'b1', revision: 1 }) }
    return Response.json(record(selected))
  }))
  const Destination = () => <p>模型入口 {useLocation().search}</p>
  render(<MemoryRouter initialEntries={['/screenings/r1']}><Routes><Route path="/screenings/:id" element={<ScreeningDetailPage />} /><Route path="/models" element={<Destination />} /></Routes></MemoryRouter>)
  await screen.findByRole('heading', { name: '银行研究' })
  expect(screen.getByRole('button', { name: `选中（${selected ? 1 : 0}）` })).toHaveAttribute('aria-pressed', 'true')
  if (!selected) {
    expect(screen.queryByRole('heading', { name: '示例银行' })).not.toBeInTheDocument()
    expect(screen.getByText('本轮没有选中的标的')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: '排除（1）' }))
  }
  expect(screen.getByText('信用风险')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: '未研究（0）' }))
  expect(screen.queryByRole('heading', { name: '示例银行' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '全部（1）' }))
  expect(screen.getByRole('heading', { name: '示例银行' })).toBeVisible()
  expect(screen.getByRole('link', { name: '公司公告' })).toHaveAttribute('href', 'https://example.org/report')
  expect(screen.getByText(/摘录.*有限证据/)).toBeVisible()
  const button = screen.getByRole('button', { name: '用于创建个股模型' })
  if (selected) {
    await userEvent.click(button)
    expect(await screen.findByText(/模型入口.*basket=b1/)).toBeVisible()
    expect(posts).toEqual(['/api/screenings/r1/basket'])
  } else {
    expect(button).toBeDisabled()
    expect(screen.getByText('缺少交易状态证据')).toBeVisible()
    expect(posts).toEqual([])
  }
})

test('list displays count and refresh failure without pretending empty success', async () => {
  let fail = false
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    if (String(input) === '/api/session') return Response.json({ token: 't', api_base: '/api' })
    if (fail) return Response.json({ message: '证据文件损坏' }, { status: 422 })
    return Response.json({ items: [{ id: 'r1', name: '银行研究', as_of: '2026-10-13T08:00:00Z', selected_count: 1, candidate_count: 3, shortfall: 1 }], next_cursor: null })
  }))
  render(<MemoryRouter><ScreeningsPage /></MemoryRouter>)
  expect(await screen.findByRole('link', { name: '银行研究' })).toBeVisible()
  expect(screen.getByText(/入选 1.*研究范围 3/)).toBeVisible()
  fail = true
  await userEvent.click(screen.getByRole('button', { name: '刷新结果' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('证据文件损坏')
})
