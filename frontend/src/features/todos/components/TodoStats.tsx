import { Card, Text } from '../../../design-system'
import { useTodoStatsQuery } from '../hooks/useTodoStatsQuery'

export function TodoStats() {
  const { data } = useTodoStatsQuery()
  if (!data) return null

  const items = [
    ['Total', data.total],
    ['Active', data.active],
    ['Completed', data.completed],
    ['Overdue', data.overdue],
  ] as const

  return (
    <section className="row" aria-label="Task statistics">
      {items.map(([label, value]) => (
        <Card key={label}>
          <Text muted size="small">
            {label}
          </Text>
          <Text>{value}</Text>
        </Card>
      ))}
    </section>
  )
}
