/**
 * Invitation-domain hooks for the dashboard's Members page.
 *
 * The accept-invitation flow (preview + consume) lives in
 * src/lib/auth.ts since it's part of the post-sign-in onboarding,
 * not the org-management surface. This module covers the
 * admin/owner-facing side: mint pending invites, list them, cancel.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError } from './api'
import type { Role } from './orgs'

/** Mirror of platform/api/schemas.py:InvitationRead. ``id`` is exposed
 * so the dashboard's Cancel button has a row to address — the strict
 * email-match guard on accept is what stops the id from being an
 * impersonation vector. */
export interface InvitationRead {
  id: string
  email: string
  role: Role
  invited_by_email: string
  expires_at: string
  created_at: string
}

function invitesKey(orgId: string | null) {
  return ['org-invitations', orgId] as const
}

async function fetchInvitations(orgId: string): Promise<InvitationRead[]> {
  const res = await fetch(`/v1/orgs/${orgId}/invites`, {
    credentials: 'include',
  })
  if (!res.ok) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`)
  }
  return (await res.json()) as InvitationRead[]
}

/** Pending invitations for an org — admin/owner only on the backend.
 * Plain-member callers get 403; the page hides the section so they
 * never see this 403 in the wild. */
export function useInvitations(orgId: string | null) {
  return useQuery({
    queryKey: invitesKey(orgId),
    queryFn: () => fetchInvitations(orgId as string),
    enabled: !!orgId,
    // Slightly fresher than members — admin churn happens on a faster
    // cadence than membership changes during demos.
    staleTime: 15_000,
  })
}

interface CreateInvitationInput {
  orgId: string
  email: string
  role: Role
}

async function createInvitationRequest(
  input: CreateInvitationInput,
): Promise<InvitationRead> {
  const res = await fetch(`/v1/orgs/${input.orgId}/invites`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: input.email, role: input.role }),
  })
  if (!res.ok) {
    let detail: unknown
    try {
      detail = await res.json()
    } catch {
      detail = null
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`)
  }
  return (await res.json()) as InvitationRead
}

export function useCreateInvitation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: createInvitationRequest,
    onSuccess: (_invite, vars) => {
      // Refresh the pending list for the org we just invited into.
      qc.invalidateQueries({ queryKey: invitesKey(vars.orgId) })
    },
  })
}

interface RevokeInvitationInput {
  invitationId: string
  /** The org id is only used to invalidate the right cache entry — the
   * backend's DELETE /v1/invites/{id} doesn't need it in the URL. */
  orgId: string
}

async function revokeInvitationRequest(
  input: RevokeInvitationInput,
): Promise<void> {
  const res = await fetch(`/v1/invites/${input.invitationId}`, {
    method: 'DELETE',
    credentials: 'include',
  })
  if (res.status === 204) return
  let detail: unknown
  try {
    detail = await res.json()
  } catch {
    detail = null
  }
  throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`)
}

export function useRevokeInvitation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: revokeInvitationRequest,
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: invitesKey(vars.orgId) })
    },
  })
}
