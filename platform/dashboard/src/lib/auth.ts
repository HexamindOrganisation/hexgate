/**
 * Authentication hooks built on top of ``lib/api.ts``.
 *
 * Single source of truth for "is anyone signed in right now": the
 * ``useUser()`` hook wraps GET /v1/users/me in React Query so every
 * component that needs to know reads from the same cache. The
 * mutation hooks (``useLogin``, ``useRegister``, …) invalidate that
 * cache on success so the next ``useUser()`` read reflects the
 * change.
 *
 * The dashboard authenticates by carrying the ``fortify_session``
 * cookie set by /v1/auth/cookie/login or /v1/auth/google/callback.
 * There's no explicit token to thread through — ``credentials:
 * 'include'`` in lib/api.ts handles it.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, UnauthenticatedError } from './api'

/** Public shape of a User. Mirrors UserRead on the backend
 * (fastapi_users.schemas.BaseUser[str] — id, email, is_active,
 * is_superuser, is_verified). hashed_password never crosses the wire. */
export interface UserRead {
  id: string
  email: string
  is_active: boolean
  is_superuser: boolean
  is_verified: boolean
}

/** Reusable cache key so mutations can invalidate without typos. */
export const USER_QUERY_KEY = ['user', 'me'] as const

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

async function fetchMe(): Promise<UserRead | null> {
  try {
    const res = await fetch('/v1/users/me', { credentials: 'include' })
    if (res.status === 401) return null
    if (!res.ok) throw new Error(`/users/me ${res.status}`)
    return (await res.json()) as UserRead
  } catch (err) {
    // Network-level errors surface as null so the UI can render the
    // "loading … then signed-out" path without a noisy console.
    if (err instanceof TypeError) return null
    throw err
  }
}

/**
 * Returns the active user, or ``null`` if not signed in.
 *
 * Doesn't trigger the global 401-redirect (lib/api.ts) — we deliberately
 * use ``fetch`` directly here so the sign-in page can call this without
 * bouncing itself away.
 */
export function useUser() {
  const query = useQuery({
    queryKey: USER_QUERY_KEY,
    queryFn: fetchMe,
    // The auth cache is hot — every page transition checks it. 30s
    // staleness is the longest a stale "logged in" view should linger
    // after a logout from another tab.
    staleTime: 30_000,
  })
  return {
    user: query.data ?? null,
    loading: query.isLoading,
    error: query.error,
  }
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

/**
 * POST /v1/auth/cookie/login. The endpoint takes form-encoded fields
 * (OAuth2 password-flow shape) with ``username`` = email.
 */
async function loginRequest(creds: {
  email: string
  password: string
}): Promise<void> {
  const body = new URLSearchParams({
    username: creds.email,
    password: creds.password,
  })
  const res = await fetch('/v1/auth/cookie/login', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  })
  if (!res.ok) {
    const detail = await res.text().catch(() => '')
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`)
  }
}

export function useLogin() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: loginRequest,
    onSuccess: () => qc.invalidateQueries({ queryKey: USER_QUERY_KEY }),
  })
}

async function logoutRequest(): Promise<void> {
  const res = await fetch('/v1/auth/cookie/logout', {
    method: 'POST',
    credentials: 'include',
  })
  // 401 here means "you weren't logged in anyway" — same end state.
  if (!res.ok && res.status !== 401) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`)
  }
}

export function useLogout() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: logoutRequest,
    onSuccess: () => qc.invalidateQueries({ queryKey: USER_QUERY_KEY }),
  })
}

async function registerRequest(payload: {
  email: string
  password: string
}): Promise<UserRead> {
  const res = await fetch('/v1/auth/register', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
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
  return (await res.json()) as UserRead
}

export function useRegister() {
  return useMutation({ mutationFn: registerRequest })
}

async function forgotPasswordRequest(email: string): Promise<void> {
  const res = await fetch('/v1/auth/forgot-password', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
  // 202 Accepted is the success status — same response whether the
  // email exists or not (no enumeration leak).
  if (!res.ok) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`)
  }
}

export function useForgotPassword() {
  return useMutation({ mutationFn: forgotPasswordRequest })
}

async function resetPasswordRequest(payload: {
  token: string
  password: string
}): Promise<void> {
  const res = await fetch('/v1/auth/reset-password', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
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
}

export function useResetPassword() {
  return useMutation({ mutationFn: resetPasswordRequest })
}

async function verifyEmailRequest(token: string): Promise<UserRead> {
  const res = await fetch('/v1/auth/verify', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
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
  return (await res.json()) as UserRead
}

/**
 * Verify an email token, modelled as a one-shot query keyed by the
 * token itself.
 *
 * Originally a ``useMutation`` fired from a ``useEffect`` on mount —
 * but that interacts badly with React 18+ StrictMode dev. StrictMode
 * mounts the component, fires the mutation, tears the observer down,
 * remounts with a fresh observer in ``isIdle``, and never reconnects
 * to the in-flight request. The result was a permanent "Verifying…"
 * card despite the backend having already flipped ``is_verified``.
 *
 * Using ``useQuery`` puts the state on the global ``queryClient``
 * keyed by ``['verify-email', token]``, so it survives the remount
 * cycle. ``staleTime: Infinity`` + ``retry: false`` makes the
 * semantics one-shot — the token can only be consumed once, so
 * retrying or refetching would just churn 400s.
 *
 * On success, the global ``/users/me`` cache is invalidated so any
 * other open tab sees ``is_verified: true`` on its next read.
 */
export function useVerifyEmail(token: string | undefined) {
  const qc = useQueryClient()
  return useQuery({
    queryKey: ['verify-email', token],
    queryFn: async () => {
      const user = await verifyEmailRequest(token as string)
      // Same cache-bust the old useMutation onSuccess did — runs once
      // when the query resolves, never on cache reads.
      qc.invalidateQueries({ queryKey: USER_QUERY_KEY })
      return user
    },
    enabled: !!token,
    retry: false,
    staleTime: Infinity,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
}

async function requestVerifyRequest(email: string): Promise<void> {
  const res = await fetch('/v1/auth/request-verify-token', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
  if (!res.ok && res.status !== 202) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`)
  }
}

export function useRequestVerify() {
  return useMutation({ mutationFn: requestVerifyRequest })
}

// ---------------------------------------------------------------------------
// Google OAuth
// ---------------------------------------------------------------------------

/**
 * Starts the Google OAuth sign-in by redirecting the browser to the
 * Google consent URL. The /v1/auth/google/callback then sets the
 * session cookie and bounces back to the dashboard.
 *
 * Returns ``null`` when OAuth isn't configured on the backend
 * (the /authorize route 404s when FORTIFY_GOOGLE_CLIENT_ID is unset) —
 * callers hide the Google button when this returns null on mount.
 */
export async function startGoogleSignIn(): Promise<void> {
  const res = await fetch('/v1/auth/google/authorize?scopes=openid&scopes=email', {
    credentials: 'include',
  })
  if (res.status === 404) {
    throw new ApiError(404, null, 'Google sign-in is not enabled on this server')
  }
  if (!res.ok) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`)
  }
  const { authorization_url } = (await res.json()) as { authorization_url: string }
  window.location.href = authorization_url
}

/** Returns true if /v1/auth/google/authorize exists on this server. */
export async function googleOAuthAvailable(): Promise<boolean> {
  try {
    const res = await fetch(
      '/v1/auth/google/authorize?scopes=openid&scopes=email',
      { credentials: 'include', method: 'HEAD' },
    )
    return res.status !== 404
  } catch {
    return false
  }
}

// Re-export so callers don't need a second import.
export { UnauthenticatedError }
