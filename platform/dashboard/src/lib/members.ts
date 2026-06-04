/**
 * Org-member hooks. React Query reads + mutations for
 * /v1/orgs/{org_id}/members/* — list, change role, remove,
 * self-leave.
 *
 * The "always at least one owner" invariant lives on the backend
 * (services.LastOwnerError → 409); these hooks surface it as
 * ApiError instances with the message attached, and the consuming
 * UI translates to a sonner toast.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError } from './api'
import type { Role } from './orgs'

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


// ---------------------------------------------------------------------------
// Full members API — used by the /orgs/:id/members page (Step 3)
// ---------------------------------------------------------------------------


/** Mirror of platform/api/schemas.py:MemberRead. ``joined_at`` is the
 * OrganizationMember row's created_at on the backend, surfaced under
 * a friendlier name in the wire schema. */
export interface MemberRead {
  user_id: string
  email: string
  role: Role
  joined_at: string
}

function membersKey(orgId: string | null) {
  return ['org-members', orgId] as const
}

async function fetchMembers(orgId: string): Promise<MemberRead[]> {
  const res = await fetch(`/v1/orgs/${orgId}/members`, {
    credentials: 'include',
  })
  if (!res.ok) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`)
  }
  return (await res.json()) as MemberRead[]
}

/** Lists every member of an org (with their role). Disabled while
 * ``orgId`` is null so the OrgMembers page can pass the URL param
 * directly without a null guard. */
export function useMembers(orgId: string | null) {
  return useQuery({
    queryKey: membersKey(orgId),
    queryFn: () => fetchMembers(orgId as string),
    enabled: !!orgId,
    staleTime: 30_000,
  })
}

interface UpdateMemberRoleInput {
  orgId: string
  userId: string
  role: Role
}

async function updateMemberRoleRequest(
  input: UpdateMemberRoleInput,
): Promise<MemberRead> {
  const res = await fetch(
    `/v1/orgs/${input.orgId}/members/${input.userId}`,
    {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role: input.role }),
    },
  )
  if (!res.ok) {
    let detail: unknown
    try {
      detail = await res.json()
    } catch {
      detail = null
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`)
  }
  return (await res.json()) as MemberRead
}

export function useUpdateMemberRole() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: updateMemberRoleRequest,
    onSuccess: (member) => {
      // Refresh both the members list and the user's orgs list — the
      // latter carries role badges in the OrgProjectSwitcher.
      qc.invalidateQueries({ queryKey: membersKey(member.user_id ? null : null) })
      qc.invalidateQueries({ queryKey: ['org-members'] })
      qc.invalidateQueries({ queryKey: ['orgs'] })
    },
  })
}

interface RemoveMemberInput {
  orgId: string
  userId: string
}

async function removeMemberRequest(input: RemoveMemberInput): Promise<void> {
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

export function useRemoveMember() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: removeMemberRequest,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['org-members'] })
      qc.invalidateQueries({ queryKey: ['orgs'] })
    },
  })
}
