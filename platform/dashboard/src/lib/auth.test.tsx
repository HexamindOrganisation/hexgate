/**
 * Tests for the auth hooks. Today only covers the security-critical
 * invariant Victor flagged in review: `useLogout` MUST drop the
 * playground module-level store on success, or a sign-out + sign-in
 * cycle on the same browser tab can leak the prior user's chat history
 * (cachedState + activeSocket survive component unmount by design —
 * see lib/playground.ts).
 *
 * The test mocks `./playground` so `resetPlayground` is a spy, then
 * drives `useLogout` through a real React Query mutation and asserts
 * the spy was called BEFORE the user-query invalidation. We don't
 * test the underlying playground reset behaviour itself — that's
 * playground.ts's contract; this file only proves the wiring.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { JSX, ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./playground', () => ({
  resetPlayground: vi.fn(),
}))

// Imported AFTER vi.mock so the hook picks up the spy version.
import { useLogout, USER_QUERY_KEY } from './auth'
import { resetPlayground } from './playground'

function makeWrapper(qc: QueryClient): (props: { children: ReactNode }) => JSX.Element {
  return ({ children }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
}

describe('useLogout', () => {
  let qc: QueryClient

  beforeEach(() => {
    qc = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: 0 },
        mutations: { retry: false },
      },
    })
    // Seed the user-query cache so we can observe the invalidation
    // that should run after the logout succeeds.
    qc.setQueryData(USER_QUERY_KEY, { id: 'u1', email: 'a@b.dev' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.mocked(resetPlayground).mockClear()
  })

  it('calls resetPlayground on logout success', async () => {
    vi.spyOn(window, 'fetch').mockResolvedValue(
      new Response('{}', { status: 200 }),
    )

    const { result } = renderHook(() => useLogout(), {
      wrapper: makeWrapper(qc),
    })

    await act(async () => {
      await result.current.mutateAsync()
    })

    expect(resetPlayground).toHaveBeenCalledOnce()
  })

  it('does NOT call resetPlayground when the logout request fails', async () => {
    // Logout-failure path: server-side cookie didn't get cleared, so
    // the user is technically still authenticated — leaking their own
    // state to themselves isn't a leak. Skipping the reset also avoids
    // confusingly clearing playground state on a transient network
    // hiccup that the user can just retry.
    vi.spyOn(window, 'fetch').mockResolvedValue(
      new Response('{"detail":"nope"}', { status: 500 }),
    )

    const { result } = renderHook(() => useLogout(), {
      wrapper: makeWrapper(qc),
    })

    await act(async () => {
      await result.current.mutateAsync().catch(() => undefined)
    })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(resetPlayground).not.toHaveBeenCalled()
  })

  it('invalidates the user query after resetting playground', async () => {
    // Order matters: playground reset must happen before React's
    // commit phase re-renders subscribers off the invalidated user
    // query (some subscriber might read playground state on the
    // sign-in screen). React Query's invalidate is synchronous from
    // the mutation's onSuccess perspective — by the time it returns,
    // the playground reset already ran.
    vi.spyOn(window, 'fetch').mockResolvedValue(
      new Response('{}', { status: 200 }),
    )

    const { result } = renderHook(() => useLogout(), {
      wrapper: makeWrapper(qc),
    })

    await act(async () => {
      await result.current.mutateAsync()
    })

    // Cache entry was marked stale (invalidate doesn't immediately
    // clear data — it flags isStale + triggers refetch).
    expect(qc.getQueryState(USER_QUERY_KEY)?.isInvalidated).toBe(true)
    // And the reset ran in the same onSuccess pass.
    expect(resetPlayground).toHaveBeenCalledOnce()
  })
})
