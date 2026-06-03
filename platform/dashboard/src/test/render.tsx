/**
 * Test render helper — wraps the component-under-test in the same
 * providers ``main.tsx`` mounts in production (React Query + Router).
 * Keeps test files focused on the actual assertions rather than
 * boilerplate context setup.
 */

import type { ReactNode } from 'react'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderOptions } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

/** Build a fresh QueryClient per test so cached requests don't leak
 * between cases (React Query caches by reference, not by request URL). */
function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Fail fast in tests — don't auto-retry on the first 4xx; we
        // want to assert error states immediately.
        retry: false,
        staleTime: 0,
      },
      mutations: { retry: false },
    },
  })
}

interface Options extends Omit<RenderOptions, 'wrapper'> {
  initialRoute?: string
}

/** Drop-in for ``render`` from @testing-library/react with the
 * project's standard providers wrapped around the tree. */
export function renderWithProviders(
  ui: ReactNode,
  { initialRoute = '/', ...rest }: Options = {},
) {
  const qc = makeQueryClient()
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialRoute]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
    rest,
  )
}
