import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { applyTheme, isDark, SCHEME_ACCENTS, SCHEMES, useTheme } from "./theme";
import { readIndexCss } from "@/test/index-css";

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

describe("SCHEME_ACCENTS", () => {
  let css = "";
  beforeAll(async () => {
    css = await readIndexCss();
  });
  // The swatches read SCHEME_ACCENTS; the real token comes from index.css.
  // They must agree or a dot shows a colour the app never paints.
  // A top-level block (the selector starts its line), however spaced.
  const primaryIn = (selector: string): string | undefined => {
    const sel = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const block = new RegExp(`(?:^|\\n)${sel}\\s*\\{([^}]*)\\}`).exec(css)?.[1];
    return block && /--primary:\s*([^;]+);/.exec(block)?.[1].trim();
  };

  it.each(SCHEMES.filter((s) => s.id !== "blue").map((s) => s.id))(
    "%s matches its index.css token block in both modes",
    (id) => {
      expect(primaryIn(`[data-scheme="${id}"]`)).toBe(SCHEME_ACCENTS[id].light);
      expect(primaryIn(`.dark[data-scheme="${id}"]`)).toBe(
        SCHEME_ACCENTS[id].dark,
      );
    },
  );

  it("blue matches the :root and .dark defaults", () => {
    expect(primaryIn(":root")).toBe(SCHEME_ACCENTS.blue.light);
    expect(primaryIn(".dark")).toBe(SCHEME_ACCENTS.blue.dark);
  });
});
