import { useQuery } from '@tanstack/react-query'
import { todoKeys } from '../api/queryKeys'
import { todosApi } from '../api/todos'

export function useTodoStatsQuery() {
  return useQuery({
    queryKey: todoKeys.stats,
    queryFn: ({ signal }) => todosApi.stats(signal),
  })
}
