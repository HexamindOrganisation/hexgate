/**
 * Smoke tests for the /audit page.
 *
 * Four load-bearing invariants:
 *
 *   1. Picking an agent in the filter bar re-queries with `agent=` in
 *      the URL — the filter state actually reaches the API.
 *   2. The outcome KPI cards toggle the outcome filter (on → chip +
 *      `outcome=` in the decisions URL; off → chip gone).
 *   3. Clicking a table row opens the detail drawer; Esc closes it.
 *   4. Selecting a row fetches its session siblings (`session_id=`)
 *      and the "Same session" list cross-links to the sibling event.
 */

import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AuditDecisionRow } from '@/lib/api'
import { useActive } from '@/lib/active'
import { EMPTY_AUDIT_FILTERS, useAuditFilters } from '@/lib/audit-filters'
import { AuditPage } from '@/routes/Audit'
import { renderWithProviders } from '@/test/render'

const PROJECT = 'p1'

const counts = (all: number, allow: number, deny: number, appr = 0) => ({
  all, allow, deny, needs_approval: appr,
})

const SUMMARY = {
  totals: counts(10, 6, 4),
  by_agent: [
    { key: 'researcher', ...counts(9, 6, 3) },
    { key: 'scraper', ...counts(1, 0, 1) },
  ],
  by_role: [
    { key: 'analyst', ...counts(6, 6, 0) },
    // The no-role bucket arrives as a raw "" key over the wire.
    { key: '', ...counts(4, 0, 4) },
  ],
  by_tool: [{ key: 'read_file', ...counts(4, 0, 4) }],
}

const ROW: AuditDecisionRow = {
  event_id: 'evt-1',
  occurred_at: '2026-06-01T10:00:00Z',
  received_at: '2026-06-01T10:00:01Z',
  agent_name: 'researcher',
  agent_version_id: 'v1',
  session_id: 'sess-1',
  user_id: 'u1',
  tool_name: 'read_file',
  role: '',
  outcome: 'deny',
  error_type: 'policy_denied',
  reason: 'blocked by policy',
  violations: ['no-secrets'],
  hint: null,
  arguments: { path: '/etc/passwd' },
}

/** Sibling event in the same session — only reachable via the drawer's
 * "Same session" list (the main table stub returns ROW alone). */
const SIBLING: AuditDecisionRow = {
  ...ROW,
  event_id: 'evt-2',
  tool_name: 'send_email',
  outcome: 'allow',
  reason: '',
  violations: [],
}

/**
 * Same fetch-stub helper pattern as Orgs.test.tsx, extended to record
 * every requested URL (path + query) so tests can assert what filter
 * state actually reached the API.
 */
function stubFetch(): string[] {
  const calls: string[] = []
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })

  vi.spyOn(window, 'fetch').mockImplementation(
    async (input: RequestInfo | URL) => {
      const raw = typeof input === 'string' ? input : input.toString()
      const url = new URL(raw, 'http://localhost')
      calls.push(url.pathname + url.search)

      switch (url.pathname) {
        case '/v1/orgs':
          return json([
            {
              id: 'org-1',
              slug: 'acme',
              name: 'Acme Inc',
              created_at: '2026-01-01T00:00:00Z',
              role: 'owner',
            },
          ])
        case '/v1/orgs/org-1/projects':
          return json([
            {
              id: PROJECT,
              org_id: 'org-1',
              name: 'demo-project',
              created_at: '2026-01-01T00:00:00Z',
            },
          ])
        case `/v1/projects/${PROJECT}/audit/summary`:
          return json(SUMMARY)
        case `/v1/projects/${PROJECT}/audit/timeseries`:
          return json([])
        case `/v1/projects/${PROJECT}/audit/decisions`: {
          // The drawer's session drill-down vs the main table.
          if (url.searchParams.get('session_id') === 'sess-1') {
            return json({ rows: [ROW, SIBLING], total: 2, limit: 12, offset: 0 })
          }
          return json({ rows: [ROW], total: 1, limit: 40, offset: 0 })
        }
        default:
          return new Response('not found', { status: 404 })
      }
    },
  )
  return calls
}

/** Open the detail drawer by clicking ROW's line in the events table. */
async function openDrawer(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText('blocked by policy'))
  // The drawer header renders the event id — unique to the drawer.
  await screen.findByText('evt-1')
}

describe('AuditPage', () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: 'org-1', activeProjectId: PROJECT })
      // The filter store is module-global — reset so a filter dialled in
      // by test A doesn't narrow test B's queries.
      useAuditFilters.setState({ filters: EMPTY_AUDIT_FILTERS, tableLimit: 40 })
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('agent filter selection lands in the next query URL', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    renderWithProviders(<AuditPage />)

    // Wait for data to land, then open the agent select (Radix trigger)
    // and pick an option from the popup.
    await screen.findByText('blocked by policy')
    // The subtitle names the ACTIVE project — not a hardcoded constant.
    expect(await screen.findByText('demo-project')).toBeInTheDocument()

    // With no filters set, optionsQ and summaryQ hash to the same query key
    // and dedupe into ONE fetch of the unscoped summary.
    expect(
      calls.filter((u) => u === `/v1/projects/${PROJECT}/audit/summary?window=30d`),
    ).toHaveLength(1)
    // Radix puts pointer-events:none on the value span — click the trigger.
    await user.click(screen.getByText('All agents').closest('button')!)
    await user.click(await screen.findByRole('option', { name: 'researcher' }))

    await waitFor(() => {
      expect(
        calls.some(
          (u) => u.includes('/audit/decisions') && u.includes('agent=researcher'),
        ),
      ).toBe(true)
    })
    // The scoped summary (KPIs/breakdown) narrows too.
    expect(
      calls.some(
        (u) => u.includes('/audit/summary') && u.includes('agent=researcher'),
      ),
    ).toBe(true)
  })

  it('maps the empty-role bucket to "(none)" locally and queries role=', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    renderWithProviders(<AuditPage />)

    // The "" key from the wire displays as "(none)" in the dropdown…
    await screen.findByText('blocked by policy')
    await user.click(screen.getByText('All roles').closest('button')!)
    await user.click(await screen.findByRole('option', { name: '(none)' }))

    // …and selecting it sends `role=` (empty value) — no "(none)" sentinel
    // ever leaves the dashboard.
    await waitFor(() => {
      expect(
        calls.some(
          (u) => u.includes('/audit/decisions') && /[?&]role=(&|$)/.test(u),
        ),
      ).toBe(true)
    })
    expect(calls.some((u) => u.includes('(none)') || u.includes('%28none%29'))).toBe(false)
  })

  it('outcome KPI card toggles the outcome filter', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    renderWithProviders(<AuditPage />)

    await user.click(await screen.findByText('Denied'))

    // On: the decisions query narrows and the active chip appears.
    await waitFor(() => {
      expect(
        calls.some(
          (u) => u.includes('/audit/decisions') && u.includes('outcome=deny'),
        ),
      ).toBe(true)
    })
    // ActiveChips only renders when a filter is set — "Clear all" is its
    // unique anchor ("deny" alone also matches the FilterBar segment).
    expect(screen.getByText('Clear all')).toBeInTheDocument()

    // Off: clicking the same card clears the filter again.
    await user.click(screen.getByText('Denied'))
    await waitFor(() => {
      expect(screen.queryByText('Clear all')).not.toBeInTheDocument()
    })
  })

  it('row click opens the detail drawer; Esc closes it', async () => {
    stubFetch()
    const user = userEvent.setup()
    renderWithProviders(<AuditPage />)

    await openDrawer(user)
    // Drawer body renders the violation tag and the envelope section.
    expect(screen.getByText('no-secrets')).toBeInTheDocument()
    expect(screen.getByText('Envelope')).toBeInTheDocument()

    await user.keyboard('{Escape}')
    await waitFor(() => {
      expect(screen.queryByText('evt-1')).not.toBeInTheDocument()
    })
  })

  it('same-session list drills into the sibling event', async () => {
    const calls = stubFetch()
    const user = userEvent.setup()
    renderWithProviders(<AuditPage />)

    await openDrawer(user)

    // Selecting the row fires the session drill-down query…
    await waitFor(() => {
      expect(
        calls.some(
          (u) => u.includes('/audit/decisions') && u.includes('session_id=sess-1'),
        ),
      ).toBe(true)
    })
    // …whose result lists the sibling (selected event excluded).
    const sibling = await screen.findByText('send_email')
    await user.click(sibling)

    // The drawer now shows the sibling event.
    await screen.findByText('evt-2')
    expect(screen.queryByText('evt-1')).not.toBeInTheDocument()
  })
})
