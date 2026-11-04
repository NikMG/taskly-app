import { render, screen, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { useTodoStatsQuery } from '../hooks/useTodoStatsQuery'
import { TodoStats } from './TodoStats'

vi.mock('../../../design-system', () => import('../../../design-system/mocks'))
vi.mock('../hooks/useTodoStatsQuery', () => ({ useTodoStatsQuery: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

function mockStats(data: unknown) {
  vi.mocked(useTodoStatsQuery).mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useTodoStatsQuery>)
}

it('renders nothing until stats are loaded', () => {
  mockStats(undefined)
  const { container } = render(<TodoStats />)
  expect(container).toBeEmptyDOMElement()
})

it('shows each statistic next to its label', () => {
  mockStats({ total: 10, active: 6, completed: 4, overdue: 2 })
  render(<TodoStats />)
  const region = screen.getByRole('region', { name: 'Task statistics' })
  const text = within(region)
    .getAllByText(/./)
    .map((n) => n.textContent)
  expect(text).toEqual([
    'Total',
    '10',
    'Active',
    '6',
    'Completed',
    '4',
    'Overdue',
    '2',
  ])
})
