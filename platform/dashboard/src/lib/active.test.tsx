/**
 * Tests for the active-org/project zustand store + ``useProjectScoped``
 * helper.
 *
 * The store invariants:
 *
 *   1. ``setActiveOrg`` clears ``activeProjectId`` so a stale project
 *      id from the previous org doesn't leak across the switch.
 *   2. State persists across "page reloads" — the persist middleware
 *      writes localStorage, fresh imports read it back.
 *   3. setActiveProject leaves the org alone (the inverse).
 *
 * The ``useProjectScoped`` helper returns one of three statuses
 * depending on the resolved active-org/project + query loading state.
 * Project-scoped pages render off this status, so the matrix needs to
 * be pinned.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { JSX, ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useActive, useProjectScoped } from './active'

describe('useActive store', () => {
  beforeEach(() => {
    // Fresh store between tests — reset both ids to null.
    act(() => {
      useActive.setState({ activeOrgId: null, activeProjectId: null })
    })
  })

  it('starts with both ids null', () => {
    const { activeOrgId, activeProjectId } = useActive.getState()
    expect(activeOrgId).toBeNull()
    expect(activeProjectId).toBeNull()
  })

  it('setActiveOrg clears activeProjectId', () => {
    // Seed both ids — simulating a user who was deep in a project
    act(() => {
      useActive.setState({
        activeOrgId: 'old-org',
        activeProjectId: 'old-project',
      })
    })

    // Switching org must clear the stale project — the old project
    // belonged to the old org and would 403 in the new one
    act(() => {
      useActive.getState().setActiveOrg('new-org')
    })

    const { activeOrgId, activeProjectId } = useActive.getState()
    expect(activeOrgId).toBe('new-org')
    expect(activeProjectId).toBeNull()
  })

  it('setActiveProject leaves activeOrgId alone', () => {
    act(() => {
      useActive.setState({
        activeOrgId: 'org-1',
        activeProjectId: 'project-a',
      })
    })

    act(() => {
      useActive.getState().setActiveProject('project-b')
    })

    const { activeOrgId, activeProjectId } = useActive.getState()
    expect(activeOrgId).toBe('org-1')
    expect(activeProjectId).toBe('project-b')
  })

  it('persists to localStorage via zustand persist middleware', () => {
    act(() => {
      useActive.getState().setActiveOrg('persisted-org')
    })
    // Hits the same key the store registered.
    const raw = window.localStorage.getItem('fortify-active')
    expect(raw).toBeTruthy()
    const parsed = JSON.parse(raw as string)
    expect(parsed.state.activeOrgId).toBe('persisted-org')
  })
})


// ---------------------------------------------------------------------------
// useProjectScoped
// ---------------------------------------------------------------------------


/** Wrap renderHook in a fresh QueryClient — useOrgs / useProjects need
 * one to mount. Same shape as test/render.tsx; duplicated here to keep
 * this file standalone. */
function makeWrapper(): ({ children }: { children: ReactNode }) => JSX.Element {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  })
  return ({ children }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

/** Stub /v1/orgs + /v1/orgs/{id}/projects for the helper to consume. */
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
      return new Response('{}', { status: 404 })
    },
  )
}

describe('useProjectScoped', () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: null, activeProjectId: null })
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("returns 'loading' while /v1/orgs is in flight", () => {
    // No stubs set up — fetch is unmocked, default jsdom behaviour is
    // a hanging promise, so the query stays in isLoading. We only
    // assert the synchronous return value once.
    vi.spyOn(window, 'fetch').mockImplementation(
      () => new Promise(() => undefined),
    )

    const { result } = renderHook(() => useProjectScoped(), {
      wrapper: makeWrapper(),
    })

    expect(result.current.status).toBe('loading')
    expect(result.current.projectId).toBeNull()
  })

  it("returns 'no-project' once orgs load but no project is active", async () => {
    stubFetch({
      '/v1/orgs': [
        {
          id: 'org-1',
          slug: 'acme',
          name: 'Acme',
          created_at: '2026-01-01T00:00:00Z',
          role: 'owner',
        },
      ],
      '/v1/orgs/org-1/projects': [], // org has no projects yet
    })
    act(() => {
      useActive.setState({
        activeOrgId: 'org-1',
        activeProjectId: null,
      })
    })

    const { result } = renderHook(() => useProjectScoped(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => {
      expect(result.current.status).toBe('no-project')
    })
    expect(result.current.projectId).toBeNull()
  })

  it("returns 'ready' with the projectId when everything's resolved", async () => {
    stubFetch({
      '/v1/orgs': [
        {
          id: 'org-1',
          slug: 'acme',
          name: 'Acme',
          created_at: '2026-01-01T00:00:00Z',
          role: 'owner',
        },
      ],
      '/v1/orgs/org-1/projects': [
        {
          id: 'proj-1',
          org_id: 'org-1',
          name: 'production',
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
    })
    act(() => {
      useActive.setState({
        activeOrgId: 'org-1',
        activeProjectId: 'proj-1',
      })
    })

    const { result } = renderHook(() => useProjectScoped(), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => {
      expect(result.current.status).toBe('ready')
    })
    expect(result.current.projectId).toBe('proj-1')
  })
})
