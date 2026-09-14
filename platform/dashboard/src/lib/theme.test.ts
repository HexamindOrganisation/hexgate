import { afterEach, describe, expect, it, vi } from "vitest";

import { applyTheme, isDark, useTheme } from "./theme";

function stubPrefersDark(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({
      matches,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.className = "";
  delete document.documentElement.dataset.scheme;
});

describe("isDark", () => {
  it("is true for dark, false for light", () => {
    expect(isDark("dark")).toBe(true);
    expect(isDark("light")).toBe(false);
  });

  it("follows the OS for system", () => {
    stubPrefersDark(true);
    expect(isDark("system")).toBe(true);
    stubPrefersDark(false);
    expect(isDark("system")).toBe(false);
  });
});

describe("applyTheme", () => {
  it("toggles the dark class and stamps the scheme onto <html>", () => {
    applyTheme("dark", "plum");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.dataset.scheme).toBe("plum");

    applyTheme("light", "blue");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.dataset.scheme).toBe("blue");
  });
});

describe("useTheme store", () => {
  it("defaults to dark/blue and updates via setters", () => {
    const s = useTheme.getState();
    expect(s.mode).toBe("dark");
    expect(s.scheme).toBe("blue");

    s.setMode("light");
    s.setScheme("plum");
    expect(useTheme.getState().mode).toBe("light");
    expect(useTheme.getState().scheme).toBe("plum");

    // restore for other tests sharing the module-level store
    useTheme.getState().setMode("dark");
    useTheme.getState().setScheme("blue");
  });
});
