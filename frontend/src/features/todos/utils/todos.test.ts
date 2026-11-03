import { describe, expect, it } from 'vitest'
import { afterEach, beforeEach, vi } from 'vitest'
import { formatDate, isOverdue, selectTodos, validateTodo } from './todos'
import type { Todo } from '../types'

const base: Todo = {
  id: 1,
  title: 'Sprint plan',
  description: '',
  priority: 'medium',
  completed: false,
  due_date: null,
  created_at: '2026-10-01T12:00:00Z',
  updated_at: '2026-10-01T12:00:00Z',
}

describe('selectTodos', () => {
  it('combines status with a case-insensitive search in the description', () => {
    const matching = { ...base, description: 'Discuss the API' }
    const todos = [
      matching,
      { ...matching, id: 2, completed: true },
      { ...base, id: 3 },
    ]
    expect(selectTodos(todos, 'active', ' api ', 'newest')).toEqual([matching])
  })

  it('sorts by priority without changing the source array', () => {
    const high: Todo = { ...base, id: 2, priority: 'high' }
    const todos = [base, high]
    expect(selectTodos(todos, 'all', '', 'priority')).toEqual([high, base])
    expect(todos).toEqual([base, high])
  })

  it('filters completed tasks and breaks priority ties by newest id', () => {
    const done = { ...base, id: 2, completed: true }
    const done2 = { ...base, id: 3, completed: true }
    expect(
      selectTodos([base, done, done2], 'completed', '', 'priority'),
    ).toEqual([done2, done])
  })

  it('sorts by due date with undated tasks last', () => {
    const late = { ...base, id: 1, due_date: '2026-12-01' }
    const early = { ...base, id: 2, due_date: '2026-11-01' }
    const none = { ...base, id: 3 }
    expect(selectTodos([none, late, early], 'all', '', 'due')).toEqual([
      early,
      late,
      none,
    ])
  })

  it('breaks due-date ties by newest id', () => {
    const a = { ...base, id: 1, due_date: '2026-11-01' }
    const b = { ...base, id: 2, due_date: '2026-11-01' }
    expect(selectTodos([a, b], 'all', '', 'due')).toEqual([b, a])
  })

  it('sorts newest first by creation time, then by id', () => {
    const newer = { ...base, id: 2, created_at: '2026-10-02T12:00:00Z' }
    const same = { ...base, id: 3 }
    expect(selectTodos([base, newer, same], 'all', '', 'newest')).toEqual([
      newer,
      same,
      base,
    ])
  })
})

describe('validateTodo', () => {
  const valid = {
    title: 'Ok',
    description: '',
    priority: 'low' as const,
    due_date: null,
    completed: false,
  }

  it('accepts a valid task', () => {
    expect(validateTodo({ ...valid, due_date: '2026-02-28' })).toEqual({})
  })

  it('rejects blank and overlong titles and descriptions', () => {
    expect(validateTodo({ ...valid, title: '   ' }).title).toBe(
      'Enter a task title.',
    )
    expect(validateTodo({ ...valid, title: 'a'.repeat(121) }).title).toBe(
      'The title must be at most 120 characters.',
    )
    expect(validateTodo({ ...valid, title: 'a'.repeat(120) })).toEqual({})
    expect(
      validateTodo({ ...valid, description: 'a'.repeat(2001) }).description,
    ).toBe('The description must be at most 2000 characters.')
  })

  it('rejects malformed and impossible dates', () => {
    expect(validateTodo({ ...valid, due_date: '2026-02-30' }).due_date).toBe(
      'Enter a valid date.',
    )
    expect(validateTodo({ ...valid, due_date: '2026-2-3' }).due_date).toBe(
      'Enter a valid date.',
    )
  })
})

describe('isOverdue', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 9, 15, 12))
  })
  afterEach(() => vi.useRealTimers())

  it('is true only for open tasks due before today', () => {
    expect(isOverdue({ ...base, due_date: '2026-10-14' })).toBe(true)
    expect(isOverdue({ ...base, due_date: '2026-10-15' })).toBe(false)
    expect(isOverdue({ ...base, due_date: null })).toBe(false)
    expect(
      isOverdue({ ...base, due_date: '2026-10-14', completed: true }),
    ).toBe(false)
  })
})

describe('formatDate', () => {
  it('formats an ISO date in en-GB style', () => {
    expect(formatDate('2026-10-05')).toBe('5 Oct 2026')
  })
})
