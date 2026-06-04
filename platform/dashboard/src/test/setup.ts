/**
 * Vitest global setup. Registered via vite.config.ts → test.setupFiles.
 *
 * Runs before every test file's imports, so anything we mount on
 * ``window`` here is available when modules under test (like the
 * zustand active-org store) initialise.
 */

import { afterEach, beforeEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

// ---------------------------------------------------------------------------
// 1. localStorage shim
//
// jsdom 29 under vitest 4 doesn't reliably expose a working ``Storage``-
// shaped ``window.localStorage`` — the zustand persist middleware crashes
// with "storage.setItem is not a function" because the global lookup
// happens at module init time, before vitest can wire its env. We
// install a Map-backed shim directly on ``window`` here so the store
// finds something usable the moment it's imported.
// ---------------------------------------------------------------------------

function makeMemoryStorage(): Storage {
  const store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    clear() {
      store.clear()
    },
    getItem(key) {
      return store.get(key) ?? null
    },
    setItem(key, value) {
      store.set(key, String(value))
    },
    removeItem(key) {
      store.delete(key)
    },
    key(index) {
      return Array.from(store.keys())[index] ?? null
    },
  }
}

Object.defineProperty(window, 'localStorage', {
  configurable: true,
  writable: true,
  value: makeMemoryStorage(),
})

// ---------------------------------------------------------------------------
// 2. jsdom doesn't implement matchMedia — Radix UI's hooks call it.
//    Stub the shape Radix expects so dropdown/dialog mounts don't crash.
// ---------------------------------------------------------------------------

if (typeof window.matchMedia === 'undefined') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined, // deprecated but Radix still touches it
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  })
}

// ---------------------------------------------------------------------------
// 3. Per-test housekeeping
// ---------------------------------------------------------------------------

beforeEach(() => {
  // Reset the storage backing between tests so persisted state from
  // test A (e.g., an activeOrgId set during an assertion) doesn't
  // leak into test B's fresh component tree.
  window.localStorage.clear()
})

afterEach(() => {
  // @testing-library/react needs explicit cleanup with vitest — Jest
  // auto-cleans, vitest doesn't.
  cleanup()
})
