import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { applyTheme, isDark, SCHEME_ACCENTS, SCHEMES, useTheme } from "./theme";
import { FONT_SIZES } from "./utils";

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

// index.css, read from disk: Vitest stubs CSS imports, `?raw` included. Node
// modules load by name so the app tsconfig needn't pull in Node types; the
// path hangs off this file so neither the cwd nor the separator matters.
let css = "";
beforeAll(async () => {
  const [fsMod, pathMod] = ["node:fs", "node:path"];
  const { readFileSync } = await import(/* @vite-ignore */ fsMod);
  const { dirname, join } = await import(/* @vite-ignore */ pathMod);
  const here = expect.getState().testPath;
  if (!here) throw new Error("vitest did not report this test's path");
  css = readFileSync(join(dirname(here), "..", "index.css"), "utf8");
});

describe("FONT_SIZES", () => {
  it("names every --text-* size declared in index.css", () => {
    const declared = [
      ...css.matchAll(/^\s*--text-([a-z]+(?:-[a-z]+)*): /gm),
    ].map((m) => m[1]);
    expect([...FONT_SIZES].sort()).toEqual([...new Set(declared)].sort());
  });
});

describe("SCHEME_ACCENTS", () => {
  // The swatches read SCHEME_ACCENTS; the real token comes from index.css.
  // They must agree or a dot shows a colour the app never paints.
  const primaryIn = (selector: string): string | undefined => {
    const start = css.indexOf(`\n${selector} {`);
    if (start < 0) return undefined;
    const block = css.slice(start, css.indexOf("}", start));
    return /--primary: ([^;]+);/.exec(block)?.[1];
  };

  it("lists every scheme in the picker", () => {
    expect(SCHEMES.map((s) => s.id).sort()).toEqual(
      Object.keys(SCHEME_ACCENTS).sort(),
    );
  });

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
