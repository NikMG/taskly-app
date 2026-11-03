import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { todoFixture } from '../../../test/todoFixture'
import { TodoItem } from './TodoItem'

vi.mock('../../../design-system', () => import('../../../design-system/mocks'))

const noop = vi.fn()

it('shows a completed task with description and no overdue marker', () => {
  render(
    <TodoItem
      todo={{
        ...todoFixture,
        completed: true,
        description: 'Details here',
        due_date: '2000-01-01',
      }}
      busy={false}
      onEdit={noop}
      onToggle={noop}
      onDelete={noop}
    />,
  )
  expect(screen.getByText('Details here')).toBeVisible()
  expect(screen.getByText('Completed')).toBeVisible()
  expect(screen.getByRole('checkbox')).toBeChecked()
  expect(screen.getByText(/1 Jan 2000/)).toBeVisible()
  expect(screen.queryByText(/overdue/)).not.toBeInTheDocument()
})

it('marks an open past-due task as overdue', () => {
  render(
    <TodoItem
      todo={{ ...todoFixture, due_date: '2000-01-01' }}
      busy={false}
      onEdit={noop}
      onToggle={noop}
      onDelete={noop}
    />,
  )
  expect(screen.getByText(/overdue/)).toBeVisible()
})

it('forwards toggle and delete, and disables controls when busy', async () => {
  const user = userEvent.setup()
  const onToggle = vi.fn()
  const onDelete = vi.fn()
  const { rerender } = render(
    <TodoItem
      todo={todoFixture}
      busy={false}
      onEdit={noop}
      onToggle={onToggle}
      onDelete={onDelete}
    />,
  )
  await user.click(screen.getByRole('checkbox'))
  await user.click(
    screen.getByRole('button', { name: `Delete: ${todoFixture.title}` }),
  )
  expect(onToggle).toHaveBeenCalledTimes(1)
  expect(onDelete).toHaveBeenCalledTimes(1)
  rerender(
    <TodoItem
      todo={todoFixture}
      busy
      onEdit={noop}
      onToggle={onToggle}
      onDelete={onDelete}
    />,
  )
  expect(screen.getByRole('checkbox')).toBeDisabled()
  expect(
    screen.getByRole('button', { name: `Edit: ${todoFixture.title}` }),
  ).toBeDisabled()
})

it('shows an active task and forwards edit requests', async () => {
  const user = userEvent.setup()
  const onEdit = vi.fn()
  const onToggle = vi.fn()
  const onDelete = vi.fn()
  render(
    <TodoItem
      todo={todoFixture}
      busy={false}
      onEdit={onEdit}
      onToggle={onToggle}
      onDelete={onDelete}
    />,
  )
  expect(screen.getByRole('heading', { name: todoFixture.title })).toBeVisible()
  expect(screen.getByText('To do')).toBeVisible()
  expect(screen.getByText('Medium')).toBeVisible()
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  await user.click(
    screen.getByRole('button', { name: `Edit: ${todoFixture.title}` }),
  )
  expect(onEdit).toHaveBeenCalledTimes(1)
  expect(onToggle).not.toHaveBeenCalled()
  expect(onDelete).not.toHaveBeenCalled()
})
