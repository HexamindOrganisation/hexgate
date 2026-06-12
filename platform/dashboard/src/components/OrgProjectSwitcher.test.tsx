/**
 * Smoke tests for the org/project switcher.
 *
 * Three invariants worth pinning in unit tests (the rest of the UX —
 * keyboard nav, focus management, etc. — is handled by Radix and not
 * worth re-asserting here):
 *
 *   1. With orgs loaded, the active org's name appears in the trigger.
 *   2. Clicking another org in the dropdown updates the zustand store
 *      AND clears the active project.
 *   3. The "New organization" footer action opens the create dialog.
 */

import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { OrgProjectSwitcher } from '@/components/OrgProjectSwitcher'
import { useActive } from '@/lib/active'
import { renderWithProviders } from '@/test/render'

/** Stub fetch with canned responses keyed by URL. Returns 404 for
 * unknown paths so a missed wiring shows up as an obvious failure. */
function stubFetch(routes: Record<string, unknown>): void {
  vi.spyOn(window, 'fetch').mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
      // Allow `/v1/orgs?foo=bar` style matches.
      const path = url.split('?')[0]
      if (path in routes) {
        return new Response(JSON.stringify(routes[path]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('not found', { status: 404 })
    },
  )
}

const ORG_A = {
  id: 'org-a',
  slug: 'org-a',
  name: 'Org Alpha',
  created_at: '2026-01-01T00:00:00Z',
  role: 'owner' as const,
}
const ORG_B = {
  id: 'org-b',
  slug: 'org-b',
  name: 'Org Beta',
  created_at: '2026-01-02T00:00:00Z',
  role: 'member' as const,
}

describe('OrgProjectSwitcher', () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: ORG_A.id, activeProjectId: null })
    })
    stubFetch({
      '/v1/orgs': [ORG_A, ORG_B],
      '/v1/orgs/org-a/projects': [],
      '/v1/orgs/org-b/projects': [],
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows the active org name in the trigger', async () => {
    renderWithProviders(<OrgProjectSwitcher />)
    await waitFor(() => {
      expect(screen.getByText('Org Alpha')).toBeInTheDocument()
    })
  })

  it('switching org updates the store and clears the active project', async () => {
    // Seed an active project so we can assert it gets cleared.
    act(() => {
      useActive.setState({
        activeOrgId: ORG_A.id,
        activeProjectId: 'stale-project',
      })
    })

    const user = userEvent.setup()
    renderWithProviders(<OrgProjectSwitcher />)

    // Open the dropdown.
    await waitFor(() => {
      expect(screen.getByText('Org Alpha')).toBeInTheDocument()
    })
    await user.click(screen.getAllByText('Org Alpha')[0]!)

    // Click Org Beta in the dropdown.
    const betaItem = await screen.findByText('Org Beta')
    await user.click(betaItem)

    expect(useActive.getState().activeOrgId).toBe(ORG_B.id)
    // Clearing the stale project across org switches is the load-bearing
    // invariant — see active.ts comments.
    expect(useActive.getState().activeProjectId).toBeNull()
  })

  it('"New organization" opens the create dialog', async () => {
    const user = userEvent.setup()
    renderWithProviders(<OrgProjectSwitcher />)

    await waitFor(() => {
      expect(screen.getByText('Org Alpha')).toBeInTheDocument()
    })
    await user.click(screen.getAllByText('Org Alpha')[0]!)

    const newOrgItem = await screen.findByText('New organization')
    await user.click(newOrgItem)

    // CreateOrgDialog renders both a title and a submit button with
    // the text "Create organization" — assert on something unique to
    // the dialog body to avoid the multi-match getByText error.
    expect(
      await screen.findByText(/Teams in Hexgate live inside/i),
    ).toBeInTheDocument()
  })
})
