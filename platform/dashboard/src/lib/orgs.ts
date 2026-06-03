/**
 * Org-domain hooks. React Query reads + mutations for the
 * /v1/orgs/* surface.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError } from './api'

export const ROLES = ['owner', 'admin', 'member'] as const
export type Role = (typeof ROLES)[number]

/** Mirror of platform/api/schemas.py:OrgRead. ``created_at`` arrives as
 * an ISO-8601 string (JSON has no native datetime). */
export interface OrgRead {
  id: string
  slug: string
  name: string
  created_at: string
}

/** Mirror of OrgWithRole — what /v1/orgs returns per row. */
export interface OrgWithRole extends OrgRead {
  role: Role
}

const ORGS_KEY = ['orgs'] as const

async function fetchOrgs(): Promise<OrgWithRole[]> {
  const res = await fetch('/v1/orgs', { credentials: 'include' })
  if (!res.ok) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`)
  }
  return (await res.json()) as OrgWithRole[]
}

/** All orgs the caller is a member of (with their role on each). The
 * load-bearing query — the OrgProjectSwitcher reads this on mount,
 * the bootstrap effect uses it to pick a default active org. */
export function useOrgs() {
  return useQuery({
    queryKey: ORGS_KEY,
    queryFn: fetchOrgs,
    staleTime: 60_000,
  })
}

interface CreateOrgInput {
  name: string
  /** Optional — when omitted the server derives a slug from the name. */
  slug?: string
}

async function createOrgRequest(input: CreateOrgInput): Promise<OrgRead> {
  const res = await fetch('/v1/orgs', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
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
  return (await res.json()) as OrgRead
}

export function useCreateOrg() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: createOrgRequest,
    onSuccess: () => {
      // The new org won't show up in useOrgs() output without a refetch.
      qc.invalidateQueries({ queryKey: ORGS_KEY })
    },
  })
}

interface UpdateOrgInput {
  orgId: string
  name?: string
  slug?: string
}

async function updateOrgRequest(input: UpdateOrgInput): Promise<OrgRead> {
  const { orgId, ...patch } = input
  const res = await fetch(`/v1/orgs/${orgId}`, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
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
  return (await res.json()) as OrgRead
}

export function useUpdateOrg() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: updateOrgRequest,
    onSuccess: () => qc.invalidateQueries({ queryKey: ORGS_KEY }),
  })
}
