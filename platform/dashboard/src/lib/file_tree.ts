/**
 * Pure helpers backing the compose file tree — the left pane renders the
 * `policy_file` rows (a flat `{name, content}` list) as a nested tree by
 * splitting each name on `/`. The entry file `policy.yaml` is pinned at the
 * top; there is no tier axis and no folder entity (folders are derived from
 * names, exactly like a filesystem).
 */

import type { PolicyFileRead, PolicyLint } from "./api";

/** The compose entry file — the one the resolver starts from. */
export const ENTRY_FILE = "policy.yaml";

/** A file's stable identity is just its full name — keys the per-file draft
 * map, the open-tab list, and the dirty set. */
export const fileKey = (name: string): string => name;

/** Last path segment — the display name (tab label, leaf label). */
export const baseName = (name: string): string => name.split("/").pop() ?? name;

export interface FileLeaf {
  type: "file";
  /** Last path segment — the display name. */
  name: string;
  /** Full file name (may contain slashes). */
  full: string;
}

export interface FolderNode {
  type: "folder";
  name: string;
  /** Path prefix this folder represents. */
  prefix: string;
  children: TreeNode[];
}

export type TreeNode = FolderNode | FileLeaf;

/** Insert one file into the child list, creating folders as needed. */
function insert(children: TreeNode[], full: string): void {
  const segments = full.split("/").filter(Boolean);
  let level = children;
  let prefix = "";
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    if (i === segments.length - 1) {
      level.push({ type: "file", name: seg, full });
      return;
    }
    prefix = prefix ? `${prefix}/${seg}` : seg;
    let folder = level.find(
      (n): n is FolderNode => n.type === "folder" && n.name === seg,
    );
    if (!folder) {
      folder = { type: "folder", name: seg, prefix, children: [] };
      level.push(folder);
    }
    level = folder.children;
  }
}

/** Folders before files, each group alphabetically, recursively — but the
 * entry file `policy.yaml` sorts first of all at the root so it reads as the
 * project's starting point. */
function sortTree(nodes: TreeNode[], root: boolean): void {
  nodes.sort((a, b) => {
    if (root) {
      const aEntry = a.type === "file" && a.full === ENTRY_FILE;
      const bEntry = b.type === "file" && b.full === ENTRY_FILE;
      if (aEntry !== bEntry) return aEntry ? -1 : 1;
    }
    if (a.type !== b.type) return a.type === "folder" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const n of nodes) if (n.type === "folder") sortTree(n.children, false);
}

/** Build the nested tree from the flat file list. */
export function buildFileTree(files: PolicyFileRead[]): TreeNode[] {
  const roots: TreeNode[] = [];
  for (const f of files) insert(roots, f.name);
  sortTree(roots, true);
  return roots;
}

/**
 * 1-based line of a lint within the open file, or null. The loader doesn't
 * track exact positions yet, so anchor a tool-scoped lint to the line where
 * that tool key is declared (a `  <tool>:` entry under `tools:`). Whole-file
 * lints (no tool) stay unanchored and surface in the inspector's list instead.
 */
export function lintToLine(content: string, lint: PolicyLint): number | null {
  if (!lint.tool) return null;
  const lines = content.split("\n");
  const key = `${lint.tool}:`;
  for (let i = 0; i < lines.length; i++) {
    // Tool entries are always nested under `tools:`, so they're indented —
    // require leading whitespace to avoid matching a same-named top-level key.
    if (/^\s/.test(lines[i]) && lines[i].trim().startsWith(key)) return i + 1;
  }
  return null;
}
