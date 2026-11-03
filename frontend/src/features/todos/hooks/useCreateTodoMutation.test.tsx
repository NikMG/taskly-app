import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'
import { todoFixture } from '../../../test/todoFixture'
import { todoKeys } from '../api/queryKeys'
import { todosApi } from '../api/todos'
import { useCreateTodoMutation } from './useCreateTodoMutation'

vi.mock('../api/todos', () => ({ todosApi: { create: vi.fn() } }))
beforeEach(() => vi.clearAllMocks())

function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return { client, wrapper }
}

const { id: _id, ...input } = todoFixture

it('puts the saved task first without duplicating its id', async () => {
  const { client, wrapper } = setup()
  const other = { ...todoFixture, id: 1 }
  client.setQueryData(todoKeys.list, [other, { ...todoFixture, title: 'old' }])
  vi.mocked(todosApi.create).mockResolvedValue(todoFixture)
  const { result } = renderHook(() => useCreateTodoMutation(), { wrapper })
  await act(() => result.current.mutateAsync(input))
  expect(client.getQueryData(todoKeys.list)).toEqual([todoFixture, other])
})

it('creates the list when none is cached', async () => {
  const { client, wrapper } = setup()
  vi.mocked(todosApi.create).mockResolvedValue(todoFixture)
  const { result } = renderHook(() => useCreateTodoMutation(), { wrapper })
  await act(() => result.current.mutateAsync(input))
  expect(client.getQueryData(todoKeys.list)).toEqual([todoFixture])
})

it('invalidates the stats on success only', async () => {
  const { client, wrapper } = setup()
  client.setQueryData(todoKeys.stats, { total: 0 })
  vi.mocked(todosApi.create).mockRejectedValue(new Error('nope'))
  const { result } = renderHook(() => useCreateTodoMutation(), { wrapper })
  await act(() => result.current.mutateAsync(input).catch(() => undefined))
  expect(client.getQueryState(todoKeys.stats)?.isInvalidated).toBe(false)
  vi.mocked(todosApi.create).mockResolvedValue(todoFixture)
  await act(() => result.current.mutateAsync(input))
  expect(client.getQueryState(todoKeys.stats)?.isInvalidated).toBe(true)
})

it('leaves the cache unchanged on failure', async () => {
  const { client, wrapper } = setup()
  client.setQueryData(todoKeys.list, [])
  vi.mocked(todosApi.create).mockRejectedValue(new Error('nope'))
  const { result } = renderHook(() => useCreateTodoMutation(), { wrapper })
  await act(() => result.current.mutateAsync(input).catch(() => undefined))
  expect(client.getQueryData(todoKeys.list)).toEqual([])
})
