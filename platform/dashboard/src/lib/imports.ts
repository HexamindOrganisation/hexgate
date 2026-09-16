/**
 * Rewrite compose `import:` references when a file is renamed or moved.
 *
 * A compose file names the files it pulls in by path, inside an `import:` list
 * (`import: [caps/refunds.yaml]`). Renaming `caps/refunds.yaml` therefore has to
 * update every file that imported the old path, or the policy stops composing.
 *
 * Detection parses the YAML and looks only at `import:` lists, so a file that
 * merely *mentions* a path (in a comment, a description) isn't mistaken for an
 * importer. The rewrite itself is a surgical token replace rather than a
 * parse + re-emit: re-emitting would reflow the document and drop the author's
 * comments/formatting. A path is a distinctive token, and the replace is
 * boundary-guarded so it can't match a path that's a prefix/suffix of a longer
 * one (`a.yaml` inside `sub/a.yaml`).
 */

import { load } from "js-yaml";

// Characters that make up a file path; used as the token boundary.
const PATH_BOUNDARY = "[\\w./-]";

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Match `path` as a whole token: preceded by start-or-non-path-char (captured
// so it can be preserved) and not followed by a path char. Lookahead only — no
// lookbehind, which some older engines reject at construction time.
function tokenPattern(path: string, flags: string): RegExp {
  return new RegExp(
    `(^|[^${PATH_BOUNDARY.slice(1, -1)}])(${escapeRegExp(path)})(?!${PATH_BOUNDARY})`,
    flags,
  );
}

/** Every path listed in an `import:` anywhere in the parsed document. */
function collectImports(node: unknown, into: Set<string>): void {
  if (Array.isArray(node)) {
    for (const child of node) collectImports(child, into);
    return;
  }
  if (node && typeof node === "object") {
    for (const [k, v] of Object.entries(node)) {
      if (k === "import" && Array.isArray(v)) {
        for (const p of v) if (typeof p === "string") into.add(p);
      }
      collectImports(v, into);
    }
  }
}

/** The named agents declared in a compose entry file — the keys of its top-level
 * `agents:` map, sorted. Agents can only be declared in the entry (leaf files are
 * grant-only), so this is the authoritative list. Returns `[]` when the file
 * doesn't parse or declares no agents (e.g. a top-level-only policy). */
export function composeAgentNames(content: string): string[] {
  try {
    const doc = load(content);
    const agents =
      doc && typeof doc === "object"
        ? (doc as { agents?: unknown }).agents
        : undefined;
    if (!agents || typeof agents !== "object" || Array.isArray(agents))
      return [];
    return Object.keys(agents).sort();
  } catch {
    return [];
  }
}

/** Whether `content` imports `path` via an `import:` list. Falls back to a
 * textual token scan when the file doesn't parse, so a real importer with a
 * YAML quirk isn't missed. */
export function referencesImport(content: string, path: string): boolean {
  try {
    const found = new Set<string>();
    collectImports(load(content), found);
    return found.has(path);
  } catch {
    return tokenPattern(path, "").test(content);
  }
}

/** `content` with every whole-token occurrence of `oldPath` replaced by
 * `newPath`. A no-op when the path isn't referenced. */
export function rewriteImportPath(
  content: string,
  oldPath: string,
  newPath: string,
): string {
  return content.replace(
    tokenPattern(oldPath, "g"),
    (_m, pre: string) => `${pre}${newPath}`,
  );
}
