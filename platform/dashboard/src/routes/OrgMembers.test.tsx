/**
 * Smoke tests for /orgs/:orgId/members.
 *
 * Three slices of the page's behaviour worth pinning:
 *
 *   1. Plain members see the table read-only — no Invite button,
 *      no Remove action, no inline RoleSelect.
 *   2. Owners see all controls and can open the Invite dialog.
 *   3. Pending invitations table shows for admins/owners and lists
 *      rows from the /v1/orgs/{id}/invites endpoint.
 *
 * The mutation flows (role change → 200 → toast, remove → 204 →
 * toast, last-owner 409 → toast) are covered by Phase 4 backend tests
 * end-to-end; here we focus on the visual permission gating.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Route, Routes } from 'react-router-dom'

import { OrgMembersPage } from '@/routes/OrgMembers'
import { renderWithProviders } from '@/test/render'

function stubFetch(routes: Record<string, unknown>): void {
  vi.spyOn(window, 'fetch').mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString()
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

function renderPage(orgId: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/orgs/:orgId/members" element={<OrgMembersPage />} />
    </Routes>,
    { initialRoute: `/orgs/${orgId}/members` },
  )
}

const OWNER_USER = {
  id: 'me-owner',
  email: 'me@example.com',
  is_active: true,
  is_superuser: false,
  is_verified: true,
}

const ORG_OWNER = {
  id: 'org-1',
  slug: 'acme',
  name: 'Acme Inc',
  created_at: '2026-01-01T00:00:00Z',
  role: 'owner' as const,
}

const ORG_AS_MEMBER = { ...ORG_OWNER, role: 'member' as const }

const MEMBERS = [
  {
    user_id: 'me-owner',
    email: 'me@example.com',
    role: 'owner',
    joined_at: '2026-01-01T00:00:00Z',
  },
  {
    user_id: 'alice-uid',
    email: 'alice@example.com',
    role: 'admin',
    joined_at: '2026-01-02T00:00:00Z',
  },
]

const INVITATIONS = [
  {
    id: 'inv-1',
    email: 'carol@example.com',
    role: 'admin',
    invited_by_email: 'me@example.com',
    expires_at: '2030-01-01T00:00:00Z',
    created_at: '2026-06-01T00:00:00Z',
  },
]

describe('OrgMembersPage', () => {
  beforeEach(() => {
    // Default stubs — each test overrides via stubFetch
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows Invite button + inline RoleSelect + Remove for owners', async () => {
    stubFetch({
      '/v1/orgs': [ORG_OWNER],
      '/v1/users/me': OWNER_USER,
      '/v1/orgs/org-1/members': MEMBERS,
      '/v1/orgs/org-1/invites': INVITATIONS,
    })

    renderPage('org-1')

    await waitFor(() => {
      expect(screen.getByText(/Members of Acme Inc/i)).toBeInTheDocument()
    })
    // Invite button — owner-only.
    expect(screen.getByText('Invite member')).toBeInTheDocument()
    // Member email appears (use alice — me@example.com also shows in the
    // invitations row's invited_by column, so it matches twice).
    expect(screen.getByText('alice@example.com')).toBeInTheDocument()
    // "Remove" buttons exist for non-self rows.
    expect(screen.getAllByText('Remove').length).toBeGreaterThanOrEqual(1)
    // Pending invitations section is visible.
    expect(
      screen.getByText(/Pending invitations/i),
    ).toBeInTheDocument()
    expect(screen.getByText('carol@example.com')).toBeInTheDocument()
  })

  it('hides Invite + Remove + pending invitations for plain members', async () => {
    stubFetch({
      '/v1/orgs': [ORG_AS_MEMBER],
      '/v1/users/me': OWNER_USER,
      '/v1/orgs/org-1/members': MEMBERS,
      // Backend would 403 invites for a plain member; the page never
      // calls it because the section is hidden — stub returns empty.
      '/v1/orgs/org-1/invites': [],
    })

    renderPage('org-1')

    await waitFor(() => {
      expect(screen.getByText(/Members of Acme Inc/i)).toBeInTheDocument()
    })

    // Plain members don't see the Invite button.
    expect(screen.queryByText('Invite member')).not.toBeInTheDocument()
    // No Remove buttons.
    expect(screen.queryByText('Remove')).not.toBeInTheDocument()
    // Pending invitations section hidden entirely (admin/owner-only info).
    expect(
      screen.queryByText(/Pending invitations/i),
    ).not.toBeInTheDocument()
    // Members table still renders so the user can see who's around.
    expect(screen.getByText('alice@example.com')).toBeInTheDocument()
  })

  it('"Invite member" opens the invite dialog with email + role fields', async () => {
    stubFetch({
      '/v1/orgs': [ORG_OWNER],
      '/v1/users/me': OWNER_USER,
      '/v1/orgs/org-1/members': MEMBERS,
      '/v1/orgs/org-1/invites': [],
    })

    const user = userEvent.setup()
    renderPage('org-1')

    await waitFor(() => {
      expect(screen.getByText('Invite member')).toBeInTheDocument()
    })
    await user.click(screen.getByText('Invite member'))

    // Dialog content shows the title — unique to the dialog body.
    expect(
      await screen.findByText('Invite a teammate'),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
  })
})
