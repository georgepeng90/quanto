import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, test, vi } from 'vitest'
import { api } from '../src/api'
import { BasketRevisionSelect } from '../src/components/BasketRevisionSelect'
import type { BasketRevisionRef } from '../src/frameworkTypes'

const baskets = ['A', 'B'].map(id => ({ basket_id: id, name: id, current_revision: 1, created_at: '' }))
const detail = (id: string) => ({ ...baskets.find(item => item.basket_id === id)!, current: { basket_id: id, revision: 1, members: [], created_at: '', selection_note: '', content_hash: id }, revisions: [{ revision: 1, content_hash: id, created_at: '' }] })
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }

function Harness() {
  const [value, setValue] = useState<BasketRevisionRef | null>(null)
  return <MemoryRouter><BasketRevisionSelect value={value} onChange={setValue} /><output>{value ? `${value.basket_id}:${value.revision}` : 'empty'}</output></MemoryRouter>
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'listBaskets').mockResolvedValue({ items: baskets, next_cursor: null })
  vi.spyOn(api, 'getBasketRevision').mockImplementation(async (id) => detail(id).current)
})

test('late basket response cannot replace a newer selection', async () => {
  const a = deferred<ReturnType<typeof detail>>(), b = deferred<ReturnType<typeof detail>>()
  vi.spyOn(api, 'getBasket').mockImplementation(id => id === 'A' ? a.promise : b.promise)
  render(<Harness />)
  const select = await screen.findByLabelText('标的组合')
  await waitFor(() => expect(screen.getByRole('option', { name: 'B' })).toBeInTheDocument())
  fireEvent.change(select, { target: { value: 'A' } })
  fireEvent.change(select, { target: { value: 'B' } })
  b.resolve(detail('B'))
  expect(await screen.findByText('B:1')).toBeInTheDocument()
  a.resolve(detail('A'))
  await waitFor(() => expect(screen.getByText('B:1')).toBeInTheDocument())
})

test('clearing a basket cancels an unfinished choice', async () => {
  const pending = deferred<ReturnType<typeof detail>>()
  vi.spyOn(api, 'getBasket').mockReturnValue(pending.promise)
  render(<Harness />)
  const select = await screen.findByLabelText('标的组合')
  await waitFor(() => expect(screen.getByRole('option', { name: 'A' })).toBeInTheDocument())
  fireEvent.change(select, { target: { value: 'A' } })
  fireEvent.change(select, { target: { value: '' } })
  pending.resolve(detail('A'))
  await waitFor(() => expect(screen.getByText('empty')).toBeInTheDocument())
})
