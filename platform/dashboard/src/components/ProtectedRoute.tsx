import { Navigate, useLocation } from 'react-router-dom'

import { useUser } from '@/lib/auth'

interface ProtectedRouteProps {
  children: React.ReactNode
}

/**
 * Wraps the authenticated dashboard. Three states:
 *
 *   - loading (initial /users/me round-trip in flight): renders a thin
 *     placeholder. Keeps the UI from flashing the sign-in screen when
 *     the user IS signed in but the cache hasn't populated yet.
 *   - signed in: renders the children (typically the AppShell + outlet).
 *   - signed out: redirects to /sign-in, remembering the requested
 *     path in the location state so the post-sign-in handler can bounce
 *     them back. ``replace`` so the history doesn't accumulate
 *     /sign-in entries when the user clicks back.
 */
export function ProtectedRoute({ children }: ProtectedRouteProps) {
  const { user, loading } = useUser()
  const location = useLocation()

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-sm text-muted-foreground">Loading…</div>
      </div>
    )
  }

  if (!user) {
    return (
      <Navigate
        to="/sign-in"
        replace
        state={{ from: location.pathname + location.search }}
      />
    )
  }

  return <>{children}</>
}
