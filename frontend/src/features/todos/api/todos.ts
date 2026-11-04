import { request } from '../../../lib/http'
import type { Todo, TodoInput, TodoStats } from '../types'

export const todosApi = {
  list: (signal?: AbortSignal) => request<Todo[]>('/todos', { signal }),
  stats: (signal?: AbortSignal) =>
    request<TodoStats>('/todos/stats', { signal }),
  create: (input: TodoInput) => {
    // A form initialized with a Todo must never send read-only fields.
    const { title, description, priority, due_date, completed } = input
    return request<Todo>('/todos', {
      method: 'POST',
      body: JSON.stringify({
        title,
        description,
        priority,
        due_date,
        completed,
      }),
    })
  },
  update: (id: number, input: Partial<TodoInput>) => {
    const { title, description, priority, due_date, completed } = input
    return request<Todo>(`/todos/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        title,
        description,
        priority,
        due_date,
        completed,
      }),
    })
  },
  remove: (id: number) => request<void>(`/todos/${id}`, { method: 'DELETE' }),
}
