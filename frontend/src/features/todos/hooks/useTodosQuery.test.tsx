import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'
import { todoFixture } from '../../../test/todoFixture'
import { todosApi } from '../api/todos'
import { useTodosQuery } from './useTodosQuery'

vi.mock('../api/todos', () => ({ todosApi: { list: vi.fn() } }))
beforeEach(() => vi.clearAllMocks())

function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return { wrapper }
}

it('loads the task list, passing the abort signal', async () => {
  vi.mocked(todosApi.list).mockResolvedValue([todoFixture])
  const { result } = renderHook(() => useTodosQuery(), setup())
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data).toEqual([todoFixture])
  expect(vi.mocked(todosApi.list).mock.calls[0][0]).toBeInstanceOf(AbortSignal)
})

it('exposes the error when loading fails', async () => {
  vi.mocked(todosApi.list).mockRejectedValue(new Error('down'))
  const { result } = renderHook(() => useTodosQuery(), setup())
  await waitFor(() => expect(result.current.isError).toBe(true))
  expect(result.current.error?.message).toBe('down')
})
