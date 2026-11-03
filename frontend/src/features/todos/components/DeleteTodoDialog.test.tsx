import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { todoFixture } from '../../../test/todoFixture'
import { useDeleteTodoMutation } from '../hooks/useDeleteTodoMutation'
import { DeleteTodoDialog } from './DeleteTodoDialog'

vi.mock('../../../design-system', () => import('../../../design-system/mocks'))
vi.mock('../hooks/useDeleteTodoMutation', () => ({
  useDeleteTodoMutation: vi.fn(),
}))

function setup(
  mutation: { isPending?: boolean; mutateAsync: () => Promise<unknown> },
  busy = false,
) {
  vi.mocked(useDeleteTodoMutation).mockReturnValue({
    isPending: false,
    error: null,
    ...mutation,
  } as unknown as ReturnType<typeof useDeleteTodoMutation>)
  const onDeleted = vi.fn()
  render(
    <DeleteTodoDialog
      todo={todoFixture}
      busy={busy}
      onClose={vi.fn()}
      onDeleted={onDeleted}
    />,
  )
  return { onDeleted }
}

it('deletes the task and reports success', async () => {
  const user = userEvent.setup()
  const mutateAsync = vi.fn().mockResolvedValue(undefined)
  const { onDeleted } = setup({ mutateAsync })
  await user.click(screen.getByRole('button', { name: 'Delete task' }))
  expect(mutateAsync).toHaveBeenCalledWith(todoFixture.id)
  expect(onDeleted).toHaveBeenCalledTimes(1)
})

it('does not report success when deletion fails', async () => {
  const user = userEvent.setup()
  const mutateAsync = vi.fn().mockRejectedValue(new Error('fail'))
  const { onDeleted } = setup({ mutateAsync })
  await user.click(screen.getByRole('button', { name: 'Delete task' }))
  expect(mutateAsync).toHaveBeenCalledTimes(1)
  expect(onDeleted).not.toHaveBeenCalled()
})

it('disables actions while a deletion is pending', () => {
  setup({ isPending: true, mutateAsync: vi.fn() })
  expect(screen.getByRole('button', { name: 'Deleting…' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
})

it('disables actions while the page is busy', () => {
  setup({ mutateAsync: vi.fn() }, true)
  expect(screen.getByRole('button', { name: 'Deleting…' })).toBeDisabled()
})

it('shows a deletion error and allows cancelling without deleting', async () => {
  const user = userEvent.setup()
  const mutateAsync = vi.fn()
  vi.mocked(useDeleteTodoMutation).mockReturnValue({
    isPending: false,
    error: new Error('Could not delete.'),
    mutateAsync,
  } as unknown as ReturnType<typeof useDeleteTodoMutation>)
  const onClose = vi.fn()
  const onDeleted = vi.fn()
  render(
    <DeleteTodoDialog
      todo={todoFixture}
      busy={false}
      onClose={onClose}
      onDeleted={onDeleted}
    />,
  )
  expect(screen.getByRole('dialog', { name: 'Delete task?' })).toBeVisible()
  expect(
    screen.getByText(
      `The task “${todoFixture.title}” will be permanently deleted.`,
    ),
  ).toBeVisible()
  expect(screen.getByTestId('alert')).toHaveTextContent('Could not delete.')
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(mutateAsync).not.toHaveBeenCalled()
  expect(onDeleted).not.toHaveBeenCalled()
})
