/**
 * Unit tests for the persisted UI-preference store.
 *
 * Two things worth pinning: the toggle actually flips the flag, and the
 * value round-trips through localStorage under the expected key so a
 * collapse survives a reload.
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useUi } from "@/lib/ui";

describe("useUi", () => {
  beforeEach(() => {
    window.localStorage.clear();
    useUi.setState({ sidebarCollapsed: false });
  });

  afterEach(() => {
    window.localStorage.clear();
  });

  it("starts expanded", () => {
    expect(useUi.getState().sidebarCollapsed).toBe(false);
  });

  it("toggleSidebar flips the flag both ways", () => {
    useUi.getState().toggleSidebar();
    expect(useUi.getState().sidebarCollapsed).toBe(true);
    useUi.getState().toggleSidebar();
    expect(useUi.getState().sidebarCollapsed).toBe(false);
  });

  it("persists the collapsed state to localStorage", () => {
    useUi.getState().toggleSidebar();
    const raw = window.localStorage.getItem("hexgate-ui");
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).state.sidebarCollapsed).toBe(true);
  });
});
