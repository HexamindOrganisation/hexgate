/**
 * Invitation-domain hooks for the dashboard.
 *
 * Two surfaces share this module:
 *
 *   - Admin/owner-facing (mint, list, cancel) — used by the
 *     /orgs/:id/members page.
 *   - Invitee-facing (preview, accept) — used by the public
 *     /invites/:id/accept landing page.
 *
 * Both call /v1/invites endpoints; bundling them keeps the type
 * definitions (Role, InvitationRead/Preview) in one place.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "./api";
import { USER_QUERY_KEY } from "./auth";
import type { MemberRead } from "./members";
import type { Role } from "./orgs";

/** Mirror of platform/api/schemas.py:InvitationRead. ``id`` is exposed
 * so the dashboard's Cancel button has a row to address — the strict
 * email-match guard on accept is what stops the id from being an
 * impersonation vector. */
export interface InvitationRead {
  id: string;
  email: string;
  role: Role;
  invited_by_email: string;
  expires_at: string;
  created_at: string;
}

function invitesKey(orgId: string | null) {
  return ["org-invitations", orgId] as const;
}

async function fetchInvitations(orgId: string): Promise<InvitationRead[]> {
  const res = await fetch(`/v1/orgs/${orgId}/invites`, {
    credentials: "include",
  });
  if (!res.ok) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as InvitationRead[];
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
  });
}

interface CreateInvitationInput {
  orgId: string;
  email: string;
  role: Role;
}

async function createInvitationRequest(
  input: CreateInvitationInput,
): Promise<InvitationRead> {
  const res = await fetch(`/v1/orgs/${input.orgId}/invites`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: input.email, role: input.role }),
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = null;
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as InvitationRead;
}

export function useCreateInvitation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createInvitationRequest,
    onSuccess: (_invite, vars) => {
      // Refresh the pending list for the org we just invited into.
      qc.invalidateQueries({ queryKey: invitesKey(vars.orgId) });
    },
  });
}

interface RevokeInvitationInput {
  invitationId: string;
  /** The org id is only used to invalidate the right cache entry — the
   * backend's DELETE /v1/invites/{id} doesn't need it in the URL. */
  orgId: string;
}

async function revokeInvitationRequest(
  input: RevokeInvitationInput,
): Promise<void> {
  const res = await fetch(`/v1/invites/${input.invitationId}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (res.status === 204) return;
  let detail: unknown;
  try {
    detail = await res.json();
  } catch {
    detail = null;
  }
  throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
}

export function useRevokeInvitation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: revokeInvitationRequest,
    onSuccess: (_, vars) => {
      qc.invalidateQueries({ queryKey: invitesKey(vars.orgId) });
    },
  });
}

// ---------------------------------------------------------------------------
// Invitee-facing: preview + accept
// ---------------------------------------------------------------------------

/** Mirror of platform/api/schemas.py:InvitationPreview. Returned by the
 * PUBLIC ``GET /v1/invites/{id}`` route so the accept landing page can
 * show "Join {org_name} as {role}" before the user signs in. The
 * invite id is UUID v4 (unguessable); the strict email-match guard on
 * the accept POST is what keeps a leaked id from being an
 * impersonation vector. */
export interface InvitationPreview {
  email: string;
  role: Role;
  invited_by_email: string;
  org_id: string;
  org_name: string;
  org_slug: string;
  expires_at: string;
}

async function fetchInvitationPreview(
  invitationId: string,
): Promise<InvitationPreview> {
  // No ``credentials: 'include'`` needed — the preview route is public.
  // We include it anyway so a signed-in user's session doesn't tickle
  // any browser "third-party cookie" path differently across this page
  // vs. the rest of the dashboard.
  const res = await fetch(`/v1/invites/${invitationId}`, {
    credentials: "include",
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = null;
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as InvitationPreview;
}

/** Fetch the public preview for an invitation id. Doesn't retry —
 * 404/410 are terminal (invite gone or expired) and re-fetching just
 * burns requests for the same answer. The accept page renders error
 * cards off ``query.error`` (an ApiError with the status code). */
export function useInvitationPreview(invitationId: string) {
  return useQuery<InvitationPreview, ApiError>({
    queryKey: ["invitation-preview", invitationId],
    queryFn: () => fetchInvitationPreview(invitationId),
    retry: false,
    // The preview is effectively immutable for the duration of an
    // accept-page visit — no need to refetch on focus or interval.
    refetchOnWindowFocus: false,
    staleTime: 60_000,
    enabled: !!invitationId,
  });
}

async function acceptInvitationRequest(
  invitationId: string,
): Promise<MemberRead> {
  const res = await fetch(`/v1/invites/${invitationId}/accept`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = null;
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as MemberRead;
}

/** Consume an invitation. Cookie-authed; the backend will 403 if the
 * caller's email doesn't match the invite. The mutation returns the
 * newly-created (or already-existing) ``MemberRead`` row so the
 * landing page can navigate straight to the joined org without
 * waiting on a ``/v1/orgs`` round-trip. */
export function useAcceptInvitation() {
  const qc = useQueryClient();
  return useMutation<MemberRead, ApiError, string>({
    mutationFn: acceptInvitationRequest,
    onSuccess: () => {
      // The user is now a member of a new org — the switcher's list
      // needs to refresh, and re-reading /users/me is cheap insurance
      // in case any verified-email side-effects fired.
      qc.invalidateQueries({ queryKey: ["orgs"] });
      qc.invalidateQueries({ queryKey: USER_QUERY_KEY });
    },
  });
}
