/**
 * Smoke tests for the invite-member dialog form.
 *
 * Two assertions:
 *
 *   1. Submitting with a valid email + role POSTs to the backend with
 *      the right body.
 *   2. Pydantic-level email validation surfaces before the request
 *      goes out (zod resolver catches it client-side).
 *
 * Toast assertions are deferred — sonner renders into a portal that
 * jsdom + RTL can't easily reach, and the toast call is already
 * happening in the component (verified by manual smoke).
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { InviteMemberDialog } from '@/components/InviteMemberDialog'
import { renderWithProviders } from '@/test/render'

describe('InviteMemberDialog', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('POSTs email + role to /v1/orgs/{id}/invites on submit', async () => {
    let captured: { url: string; body: unknown } | null = null
    vi.spyOn(window, 'fetch').mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString()
        if (init?.method === 'POST' && url.includes('/invites')) {
          const body = init.body ? JSON.parse(init.body as string) : null
          captured = { url, body }
          return new Response(
            JSON.stringify({
              id: 'new-invite',
              email: 'alice@example.com',
              role: 'admin',
              invited_by_email: 'me@example.com',
              expires_at: '2030-01-01T00:00:00Z',
              created_at: '2026-06-01T00:00:00Z',
            }),
            { status: 201 },
          )
        }
        return new Response('{}', { status: 200 })
      },
    )

    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    renderWithProviders(
      <InviteMemberDialog
        open={true}
        onOpenChange={onOpenChange}
        orgId="org-1"
      />,
    )

    await user.type(
      screen.getByLabelText('Email'),
      'alice@example.com',
    )
    await user.click(screen.getByText('Send invitation'))

    await waitFor(() => {
      expect(captured).not.toBeNull()
    })
    expect(captured!.url).toContain('/v1/orgs/org-1/invites')
    expect(captured!.body).toEqual({ email: 'alice@example.com', role: 'member' })
    // After success, the dialog asks to close.
    await waitFor(() => {
      expect(onOpenChange).toHaveBeenCalledWith(false)
    })
  })

  it('client-side validation rejects an invalid email', async () => {
    const fetchSpy = vi.spyOn(window, 'fetch')

    const user = userEvent.setup()
    renderWithProviders(
      <InviteMemberDialog
        open={true}
        onOpenChange={() => undefined}
        orgId="org-1"
      />,
    )

    await user.type(screen.getByLabelText('Email'), 'not-an-email')
    await user.click(screen.getByText('Send invitation'))

    // The load-bearing assertion: invalid form → no API call. The
    // exact error message text varies by zod version (custom message
    // in zod 3, default in zod 4), so we don't pin specific copy.
    await new Promise((r) => setTimeout(r, 50))  // let validation settle
    const inviteCalls = fetchSpy.mock.calls.filter(([url, init]) => {
      const u = typeof url === 'string' ? url : url.toString()
      return (
        u.includes('/invites') &&
        (init as RequestInit | undefined)?.method === 'POST'
      )
    })
    expect(inviteCalls).toHaveLength(0)
  })
})
