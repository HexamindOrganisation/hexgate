/**
 * Org-member hooks.
 *
 * Step 2 ships only ``useLeaveOrg`` — the minimum needed for the
 * Settings page's "Leave organization" button. The full members API
 * (list / change role / remove someone else / invite) lands in Step 3
 * when the Members tab gets built.
 */

import { useMutation, useQueryClient } from '@tanstack/react-query'

import { ApiError } from './api'

interface LeaveOrgInput {
  /** Org the caller is leaving. Resolved server-side to a 204; if the
   * caller is the only owner, backend returns 409 LastOwnerError and
   * the mutation surfaces it via :class:`ApiError`. */
  orgId: string

  /** The caller's own user id. The DELETE route accepts admin/owner
   * removing anyone OR plain members removing themselves; passing
   * ``user.id`` here uses the self-removal path. */
  userId: string
}

async function leaveOrgRequest(input: LeaveOrgInput): Promise<void> {
  const res = await fetch(
    `/v1/orgs/${input.orgId}/members/${input.userId}`,
    {
      method: 'DELETE',
      credentials: 'include',
    },
  )
  if (res.status === 204) return
  let detail: unknown
  try {
    detail = await res.json()
  } catch {
    detail = null
  }
  throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`)
}

export function useLeaveOrg() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: leaveOrgRequest,
    onSuccess: () => {
      // The org listing must drop the row the user just left so the
      // OrgProjectSwitcher refreshes correctly. The active-org
      // bootstrap effect in AppShell handles "stale activeOrgId after
      // leaving" by picking the next remaining org.
      qc.invalidateQueries({ queryKey: ['orgs'] })
    },
  })
}
