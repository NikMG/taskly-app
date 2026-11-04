import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'
import { todoKeys } from '../api/queryKeys'
import { todosApi } from '../api/todos'
import { useTodoStatsQuery } from './useTodoStatsQuery'

vi.mock('../api/todos', () => ({ todosApi: { stats: vi.fn() } }))
beforeEach(() => vi.clearAllMocks())

it('loads stats under the stats key, passing the abort signal', async () => {
  const stats = { total: 3, completed: 1, active: 2, overdue: 1 }
  vi.mocked(todosApi.stats).mockResolvedValue(stats)
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const { result } = renderHook(() => useTodoStatsQuery(), { wrapper })
  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(result.current.data).toEqual(stats)
  expect(client.getQueryData(todoKeys.stats)).toEqual(stats)
  expect(vi.mocked(todosApi.stats).mock.calls[0][0]).toBeInstanceOf(AbortSignal)
})
