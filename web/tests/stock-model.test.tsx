import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { expect, test, vi } from 'vitest'
import { ModelCreation } from '../src/components/ModelCreation'
import type { InvestingOverview } from '../src/investingTypes'

test.each([true, false])('stock model shows matching plans and creation remains separate from training (ready=%s)', async ready => {
  const posts: string[] = []
  const stockRef = { kind: 'learning', id: 'stock-ppo-cash-log', version: '1' }
  const tradingRef = { kind: 'trading', id: 'stock-daily', version: '1' }
  const revision = { basket_id: 'stock', revision: 1, members: [{ exchange: 'SZSE', instrument_type: 'stock', code: '000001' }], selection_note: 'research' }
  const schema = { type: 'object', properties: { lookback: { type: 'integer', minimum: 3 }, frequency: { type: 'string', enum: ['weekly', 'monthly'] } } }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input)
    if (path === '/api/session') return Response.json({ token: 'test', api_base: '/api' })
    if (init?.method === 'POST') {
      posts.push(path)
      if (path === '/api/capabilities/check') return Response.json({ operation: 'train', status: ready ? 'ready' : 'blocked', checks: ready ? [] : [
        { name: 'stock_execution_evidence', status: 'blocked', code: 'DATA_REQUIREMENT_UNSATISFIED', message: '缺少开盘交易状态证据', field_path: 'dataset' }], assumptions: [], limitations: [] })
      return Response.json({ id: 'saved' })
    }
    if (path === '/api/definitions') return Response.json({ items: [
      { ref: stockRef, supported_instrument_types: ['stock'], parameter_schema: schema, allowed_trading_refs: [tradingRef], action_version: 'hold-target-cash-quartiles-v2', reward_version: 'net-log-return-v1' },
      { ref: { ...stockRef, id: 'stock-ppo-cash-state-log' }, supported_instrument_types: ['stock'], parameter_schema: schema, allowed_trading_refs: [tradingRef], action_version: 'hold-target-cash-quartiles-v2', reward_version: 'net-log-return-with-state-v1' },
      { ref: { ...stockRef, id: 'stock-ppo-cash-dsr' }, supported_instrument_types: ['stock'], parameter_schema: schema, allowed_trading_refs: [tradingRef], action_version: 'hold-target-cash-quartiles-v2', reward_version: 'differential-sharpe-v1' },
      { ref: tradingRef, supported_instrument_types: ['stock'], parameter_schema: { type: 'object', properties: {} } },
      { ref: { ...stockRef, id: 'etf-ppo-cash-log' }, supported_instrument_types: ['etf'], parameter_schema: schema },
    ] })
    if (path === '/api/baskets') return Response.json({ items: [{ basket_id: 'stock', name: '公司组合', current_revision: 1 }], next_cursor: null })
    if (path === '/api/baskets/stock') return Response.json({ current_revision: 1, current: revision })
    if (path === '/api/baskets/stock/revisions/1') return Response.json(revision)
    throw new Error(path)
  }))
  const created = vi.fn()
  const overview = { capabilities: { cuda_available: false }, datasets: [{ id: 'd1', manifest: { source: '已归档个股行情', data_start: '2021-01-01', data_end: '2024-01-01' } }] } as InvestingOverview
  render(<MemoryRouter><ModelCreation overview={overview} onCreated={created} /></MemoryRouter>)
  await userEvent.selectOptions(await screen.findByLabelText('标的组合'), 'stock')
  await screen.findByText(/组合成员/)
  expect(screen.queryByRole('option', { name: 'etf-ppo-cash-log' })).not.toBeInTheDocument()
  expect(await screen.findByRole('option', { name: '个股量价 PPO' })).toBeVisible()
  expect(screen.getByRole('option', { name: '个股净对数收益 PPO（带状态）' })).toBeVisible()
  expect(screen.getByRole('option', { name: '个股 DSR PPO（差分夏普）' })).toBeVisible()
  await userEvent.selectOptions(screen.getByLabelText('日线数据版本'), 'd1')
  await userEvent.selectOptions(screen.getByLabelText('学习定义'), JSON.stringify(['learning', stockRef.id, '1']))
  expect(screen.getByRole('option', { name: '个股量价 PPO' })).toBeVisible()
  await userEvent.selectOptions(screen.getByLabelText('交易定义'), JSON.stringify(['trading', tradingRef.id, '1']))
  fireEvent.change(screen.getByLabelText('lookback'), { target: { value: '30' } })
  await userEvent.selectOptions(screen.getByLabelText('frequency'), '0')
  fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: '公司模型' } })
  for (const [label, value] of [['训练开始', '2021-01-01'], ['训练结束', '2022-12-31'], ['验证开始', '2023-01-01'], ['验证结束', '2023-12-31']])
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  await userEvent.click(screen.getByRole('button', { name: '检查运行能力' }))
  if (ready) {
    await waitFor(() => expect(screen.getByRole('button', { name: '创建模型' })).toBeEnabled())
    await userEvent.click(screen.getByRole('button', { name: '创建模型' }))
    expect(created).toHaveBeenCalled()
  } else {
    await screen.findByText(/缺少开盘交易状态证据/, { selector: 'li' })
    expect(screen.getByRole('button', { name: '创建模型' })).toBeDisabled()
  }
  expect(posts.some(path => path.endsWith('/train'))).toBe(false)
})
