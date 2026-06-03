/**
 * Tests for the active-org/project zustand store.
 *
 * Three invariants:
 *
 *   1. ``setActiveOrg`` clears ``activeProjectId`` so a stale project
 *      id from the previous org doesn't leak across the switch.
 *   2. State persists across "page reloads" — the persist middleware
 *      writes localStorage, fresh imports read it back.
 *   3. setActiveProject leaves the org alone (the inverse).
 */

import { act } from '@testing-library/react'
import { describe, expect, it, beforeEach } from 'vitest'

import { useActive } from './active'

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
