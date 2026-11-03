import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { expect, it } from 'vitest'
import { todoKeys } from '../api/queryKeys'
import { useTodosBusy } from './useTodosBusy'

it('is busy only while a todo mutation is pending', async () => {
  const client = new QueryClient()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const { result } = renderHook(() => useTodosBusy(), { wrapper })
  expect(result.current).toBe(false)

  let release: () => void = () => undefined
  const pending = client
    .getMutationCache()
    .build(client, {
      mutationKey: todoKeys.update,
      mutationFn: () => new Promise<void>((resolve) => (release = resolve)),
    })
    .execute(undefined)
  await waitFor(() => expect(result.current).toBe(true))
  release()
  await pending
  await waitFor(() => expect(result.current).toBe(false))
})
