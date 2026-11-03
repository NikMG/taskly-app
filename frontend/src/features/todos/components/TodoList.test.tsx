import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { todoFixture } from '../../../test/todoFixture'
import { TodoList } from './TodoList'

vi.mock('../../../design-system', () => import('../../../design-system/mocks'))
vi.mock('./TodoEmptyState', () => ({
  TodoEmptyState: () => <p>Empty task list</p>,
}))
vi.mock('./TodoItem', () => ({
  TodoItem: ({
    todo,
    busy,
    onToggle,
    onEdit,
    onDelete,
  }: {
    todo: { title: string }
    busy: boolean
    onToggle: () => void
    onEdit: () => void
    onDelete: () => void
  }) => (
    <li>
      <button onClick={onToggle}>{`Toggle ${todo.title}`}</button>
      <button onClick={onEdit}>Edit</button>
      <button onClick={onDelete}>Delete</button>
      {busy && 'busy'}
    </li>
  ),
}))

const listProps = {
  todos: [],
  hasTodos: false,
  loading: false,
  failed: false,
  busy: false,
  onCreate: vi.fn(),
  onToggle: vi.fn(),
  onEdit: vi.fn(),
  onDelete: vi.fn(),
}

it('shows an unavailable message when loading failed with no cached tasks', () => {
  render(<TodoList {...listProps} failed />)
  expect(
    screen.getByRole('heading', {
      name: 'The task list is temporarily unavailable',
    }),
  ).toBeVisible()
})

it('keeps showing cached tasks when a refetch failed', () => {
  render(
    <TodoList
      {...listProps}
      failed
      hasTodos
      todos={[{ ...todoFixture, title: 'Cached' }]}
    />,
  )
  expect(screen.getByText('Toggle Cached')).toBeVisible()
})

it('forwards edit and delete for the chosen task', async () => {
  const user = userEvent.setup()
  const onEdit = vi.fn()
  const onDelete = vi.fn()
  render(
    <TodoList
      {...listProps}
      hasTodos
      todos={[todoFixture]}
      onEdit={onEdit}
      onDelete={onDelete}
    />,
  )
  await user.click(screen.getByRole('button', { name: 'Edit' }))
  await user.click(screen.getByRole('button', { name: 'Delete' }))
  expect(onEdit).toHaveBeenCalledWith(todoFixture)
  expect(onDelete).toHaveBeenCalledWith(todoFixture)
})

it('renders each task and binds callbacks to that task', async () => {
  const user = userEvent.setup()
  const onToggle = vi.fn()
  const second = { ...todoFixture, id: 8, title: 'Second' }
  render(
    <TodoList
      {...listProps}
      busy
      hasTodos
      onToggle={onToggle}
      todos={[todoFixture, second]}
    />,
  )
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  await user.click(screen.getByRole('button', { name: 'Toggle Second' }))
  expect(onToggle).toHaveBeenCalledWith(second)
  expect(screen.getAllByText('busy')).toHaveLength(2)
})

it('shows loading feedback before showing an empty list', () => {
  const props = {
    todos: [],
    hasTodos: false,
    failed: false,
    busy: false,
    onCreate: vi.fn(),
    onToggle: vi.fn(),
    onEdit: vi.fn(),
    onDelete: vi.fn(),
  }
  const { rerender } = render(<TodoList {...props} loading />)
  expect(screen.getByRole('status')).toHaveTextContent('Loading your tasks…')
  expect(screen.queryByText('Empty task list')).not.toBeInTheDocument()
  rerender(<TodoList {...props} loading={false} />)
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  expect(screen.getByText('Empty task list')).toBeVisible()
})
