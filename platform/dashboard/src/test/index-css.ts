import { expect } from "vitest";

/** src/index.css, read from disk: Vitest stubs CSS imports, `?raw` included.
 * Node modules load by name so the app tsconfig needn't pull in Node types;
 * the path is the calling test's own up to `src/`, so any test, at any depth,
 * finds it whatever the cwd. */
export async function readIndexCss(): Promise<string> {
  const [fsMod, pathMod] = ["node:fs", "node:path"];
  const { readFileSync } = await import(/* @vite-ignore */ fsMod);
  const { join } = await import(/* @vite-ignore */ pathMod);
  const here = expect.getState().testPath;
  // Either separator: Vitest reports forward slashes even on Windows.
  const at = here
    ? Math.max(here.lastIndexOf("/src/"), here.lastIndexOf("\\src\\"))
    : -1;
  if (!here || at < 0) throw new Error(`no src/ in this test's path: ${here}`);
  return readFileSync(join(here.slice(0, at + 5), "index.css"), "utf8");
}
