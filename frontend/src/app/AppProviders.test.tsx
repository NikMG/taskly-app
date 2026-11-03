import { QueryClient, useQueryClient } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { useTheme } from 'styled-components'
import { expect, it } from 'vitest'
import { queryClient } from '../lib/queryClient'
import { AppProviders } from './AppProviders'

function Probe({ expected }: { expected: QueryClient }) {
  const client = useQueryClient()
  const theme = useTheme()
  return (
    <p>
      {client === expected ? 'client ok' : 'client wrong'}
      {theme && Object.keys(theme).length > 0 ? ' theme ok' : ' theme missing'}
    </p>
  )
}

it('supplies the shared query client and a theme by default', () => {
  render(
    <AppProviders>
      <Probe expected={queryClient} />
    </AppProviders>,
  )
  expect(screen.getByText('client ok theme ok')).toBeInTheDocument()
})

it('uses a client passed in', () => {
  const client = new QueryClient()
  render(
    <AppProviders client={client}>
      <Probe expected={client} />
    </AppProviders>,
  )
  expect(screen.getByText('client ok theme ok')).toBeInTheDocument()
})
