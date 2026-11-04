import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { useNotice } from '../../hooks/useNotice'
import { todoFixture } from '../../test/todoFixture'
import type { Todo } from './types'
import { useTodoFilters } from './hooks/useTodoFilters'
import { useTodosBusy } from './hooks/useTodosBusy'
import { useTodosQuery } from './hooks/useTodosQuery'
import { useUpdateTodoMutation } from './hooks/useUpdateTodoMutation'
import { TodosPage } from './TodosPage'

vi.mock('../../design-system', () => import('../../design-system/mocks'))
vi.mock('../../hooks/useNotice', () => ({ useNotice: vi.fn() }))
vi.mock('./hooks/useTodoFilters', () => ({ useTodoFilters: vi.fn() }))
vi.mock('./hooks/useTodosBusy', () => ({ useTodosBusy: vi.fn() }))
vi.mock('./hooks/useTodosQuery', () => ({ useTodosQuery: vi.fn() }))
vi.mock('./hooks/useUpdateTodoMutation', () => ({
  useUpdateTodoMutation: vi.fn(),
}))
vi.mock('./components/TodosHeader', () => ({
  TodosHeader: ({ onCreate }: { onCreate: () => void }) => (
    <>
      <h1>Tasks</h1>
      <button onClick={onCreate}>New task</button>
    </>
  ),
}))
vi.mock('./components/TodoStats', () => ({ TodoStats: () => null }))
vi.mock('./components/TodoFilters', () => ({ TodoFilters: () => null }))
vi.mock('./components/TodoList', () => ({
  TodoList: ({
    todos,
    onToggle,
    onEdit,
    onDelete,
    onCreate,
  }: {
    todos: Todo[]
    onToggle: (todo: Todo) => void
    onEdit: (todo: Todo) => void
    onDelete: (todo: Todo) => void
    onCreate: () => void
  }) => (
    <>
      <p>Task list</p>
      <button onClick={onCreate}>Create</button>
      {todos[0] && (
        <>
          <button onClick={() => onToggle(todos[0])}>Toggle</button>
          <button onClick={() => onEdit(todos[0])}>Edit</button>
          <button onClick={() => onDelete(todos[0])}>Delete</button>
        </>
      )}
    </>
  ),
}))
vi.mock('./components/TodoEditor', () => ({
  TodoEditor: ({
    todo,
    onSaved,
    onClose,
  }: {
    todo: Todo | 'new'
    onSaved: (message: string) => void
    onClose: () => void
  }) => (
    <div>
      <p>{`Editor: ${todo === 'new' ? 'new' : todo.title}`}</p>
      <button onClick={() => onSaved('Saved!')}>Save editor</button>
      <button onClick={onClose}>Close editor</button>
    </div>
  ),
}))
vi.mock('./components/DeleteTodoDialog', () => ({
  DeleteTodoDialog: ({
    todo,
    onDeleted,
    onClose,
  }: {
    todo: Todo
    onDeleted: () => void
    onClose: () => void
  }) => (
    <div>
      <p>{`Deleting: ${todo.title}`}</p>
      <button onClick={onDeleted}>Confirm delete</button>
      <button onClick={onClose}>Close delete</button>
    </div>
  ),
}))
vi.mock('../../components/SuccessNotice', () => ({ SuccessNotice: () => null }))

function setupToggle(options: { busy: boolean; saved: boolean }) {
  const mutateAsync = vi.fn().mockResolvedValue({ completed: options.saved })
  const notify = vi.fn()
  vi.mocked(useTodosQuery).mockReturnValue({
    data: [todoFixture],
    isPending: false,
    isFetching: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof useTodosQuery>)
  vi.mocked(useUpdateTodoMutation).mockReturnValue({
    error: null,
    reset: vi.fn(),
    mutateAsync,
  } as unknown as ReturnType<typeof useUpdateTodoMutation>)
  vi.mocked(useTodosBusy).mockReturnValue(options.busy)
  vi.mocked(useNotice).mockReturnValue({ message: '', notify })
  vi.mocked(useTodoFilters).mockReturnValue({
    filter: 'all',
    search: '',
    sort: 'newest',
    visible: [todoFixture],
    setFilter: vi.fn(),
    setSearch: vi.fn(),
    setSort: vi.fn(),
  })
  return { mutateAsync, notify }
}

it('toggles a task and congratulates on completion', async () => {
  const user = userEvent.setup()
  const { mutateAsync, notify } = setupToggle({ busy: false, saved: true })
  render(<TodosPage />)
  await user.click(screen.getByRole('button', { name: 'Toggle' }))
  expect(mutateAsync).toHaveBeenCalledWith({
    id: todoFixture.id,
    input: { completed: true },
  })
  expect(notify).toHaveBeenCalledWith('Another task complete. Well done!')
})

it('announces when a task is marked active again', async () => {
  const user = userEvent.setup()
  const { notify } = setupToggle({ busy: false, saved: false })
  render(<TodosPage />)
  await user.click(screen.getByRole('button', { name: 'Toggle' }))
  expect(notify).toHaveBeenCalledWith('Task marked as active.')
})

it('opens the editor for a new or existing task and notifies on save', async () => {
  const user = userEvent.setup()
  const { notify } = setupToggle({ busy: false, saved: true })
  render(<TodosPage />)
  await user.click(screen.getByRole('button', { name: 'Create' }))
  expect(screen.getByText('Editor: new')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Close editor' }))
  expect(screen.queryByText(/Editor:/)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Edit' }))
  expect(screen.getByText(`Editor: ${todoFixture.title}`)).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Save editor' }))
  expect(notify).toHaveBeenCalledWith('Saved!')
  expect(screen.queryByText(/Editor:/)).not.toBeInTheDocument()
})

it('opens the editor from the header button', async () => {
  const user = userEvent.setup()
  setupToggle({ busy: false, saved: true })
  render(<TodosPage />)
  await user.click(screen.getByRole('button', { name: 'New task' }))
  expect(screen.getByText('Editor: new')).toBeVisible()
})

it('confirms deletion for the chosen task and notifies when deleted', async () => {
  const user = userEvent.setup()
  const { notify } = setupToggle({ busy: false, saved: true })
  render(<TodosPage />)
  await user.click(screen.getByRole('button', { name: 'Delete' }))
  expect(screen.getByText(`Deleting: ${todoFixture.title}`)).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Close delete' }))
  expect(screen.queryByText(/Deleting:/)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Delete' }))
  await user.click(screen.getByRole('button', { name: 'Confirm delete' }))
  expect(notify).toHaveBeenCalledWith('Task deleted.')
  expect(screen.queryByText(/Deleting:/)).not.toBeInTheDocument()
})

it('ignores a toggle while another change is in progress', async () => {
  const user = userEvent.setup()
  const { mutateAsync } = setupToggle({ busy: true, saved: true })
  render(<TodosPage />)
  await user.click(screen.getByRole('button', { name: 'Toggle' }))
  expect(mutateAsync).not.toHaveBeenCalled()
})

it('displays a query failure and resets mutation errors before retrying', async () => {
  const user = userEvent.setup()
  const refetch = vi.fn().mockResolvedValue(undefined)
  const reset = vi.fn()
  vi.mocked(useTodosQuery).mockReturnValue({
    data: [],
    isPending: false,
    isFetching: false,
    isError: true,
    error: new Error('The server is unavailable.'),
    refetch,
  } as unknown as ReturnType<typeof useTodosQuery>)
  vi.mocked(useUpdateTodoMutation).mockReturnValue({
    error: null,
    reset,
    mutateAsync: vi.fn(),
  } as unknown as ReturnType<typeof useUpdateTodoMutation>)
  vi.mocked(useTodosBusy).mockReturnValue(false)
  vi.mocked(useNotice).mockReturnValue({ message: '', notify: vi.fn() })
  vi.mocked(useTodoFilters).mockReturnValue({
    filter: 'all',
    search: '',
    sort: 'newest',
    visible: [],
    setFilter: vi.fn(),
    setSearch: vi.fn(),
    setSort: vi.fn(),
  })
  render(<TodosPage />)
  expect(screen.getByTestId('alert')).toHaveTextContent(
    'The server is unavailable.',
  )
  await user.click(screen.getByRole('button', { name: 'Try again' }))
  await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1))
  expect(reset).toHaveBeenCalledTimes(1)
  expect(reset.mock.invocationCallOrder[0]).toBeLessThan(
    refetch.mock.invocationCallOrder[0],
  )
})
