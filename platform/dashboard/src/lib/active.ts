/**
 * Active-org / active-project state for the dashboard.
 *
 * Two ids that every project-scoped page reads from. Persisted to
 * localStorage so refreshing the tab keeps the user in the same
 * workspace; resets to null on logout (handled by the cookie
 * lifecycle — fresh cookie → fresh store).
 *
 * Phase 5 step 1 keeps this URL-independent; the source of truth is
 * the store, set by the OrgProjectSwitcher and the bootstrap effect.
 * A future milestone may swap to URL-routed (`/orgs/:slug/...`) at
 * which point this becomes a derived value rather than a setter
 * destination.
 */

import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

import { useOrgs } from './orgs'
import { useProjects } from './projects'

interface ActiveState {
  /** Currently-active organization id (UUID), or null when the user
   * is signed out / has no orgs (impossible after Phase 4 step 1's
   * auto-default-org-on-signup, but defensive). */
  activeOrgId: string | null

  /** Currently-active project within the active org. Null when the
   * active org has no projects yet — the dashboard's project-scoped
   * pages render an empty state until the user creates one. */
  activeProjectId: string | null

  setActiveOrg: (orgId: string | null) => void
  setActiveProject: (projectId: string | null) => void
}

export const useActive = create<ActiveState>()(
  persist(
    (set) => ({
      activeOrgId: null,
      activeProjectId: null,
      setActiveOrg: (orgId) => {
        // Changing org always clears the project — the project belongs
        // to a specific org, so a project id from the old org would be
        // a security trap if it survived the swap (the route 403s
        // anyway, but better to surface as "select a project" than
        // "you don't have access").
        set({ activeOrgId: orgId, activeProjectId: null })
      },
      setActiveProject: (projectId) => set({ activeProjectId: projectId }),
    }),
    {
      name: 'hexgate-active',
      // ``createJSONStorage`` lazy-resolves localStorage at call time
      // rather than at module-import time. Without it, importing this
      // module from a unit test (before jsdom finishes wiring its
      // window globals) crashes with "storage.setItem is not a
      // function". Persisting both ids means refresh re-lands the
      // user on the same screen.
      storage: createJSONStorage(() => localStorage),
    },
  ),
)


/**
 * Resolved view of "what project is the dashboard currently scoped to?"
 *
 * Three states:
 *   - ``loading``    — orgs or projects still fetching
 *   - ``no-project`` — active org has no projects yet (or no org)
 *   - ``ready``      — ``projectId`` is non-null and usable in URLs
 *
 * Every project-scoped page (Tokens, Agents, Policies, Graph,
 * Playground) calls this and renders one of three branches off the
 * status. Keeps the "are we ready to fetch?" plumbing in a single
 * helper rather than re-deriving it on each page.
 *
 * The AppShell bootstrap effect normally populates ``activeProjectId``
 * before any project-scoped page mounts, so ``loading`` and
 * ``no-project`` are the edge cases (first visit, mid-org-switch, or a
 * user whose org happens to have zero projects).
 */
export interface ProjectScope {
  /** Resolved active project id, or null when not ready. */
  projectId: string | null
  status: 'loading' | 'no-project' | 'ready'
}

export function useProjectScoped(): ProjectScope {
  const activeOrgId = useActive((s) => s.activeOrgId)
  const activeProjectId = useActive((s) => s.activeProjectId)
  // useOrgs() is the load-bearing query — once it resolves, the
  // AppShell bootstrap kicks in and picks a default project. We touch
  // it here mostly to surface its ``isLoading`` state; the cache is
  // already populated by the time any project-scoped page mounts.
  const orgsQuery = useOrgs()
  const projectsQuery = useProjects(activeOrgId)

  if (
    orgsQuery.isLoading ||
    (activeOrgId !== null && projectsQuery.isLoading)
  ) {
    return { projectId: null, status: 'loading' }
  }
  if (!activeProjectId) {
    return { projectId: null, status: 'no-project' }
  }
  return { projectId: activeProjectId, status: 'ready' }
}
