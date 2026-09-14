/**
 * Per-device empty folders for the compose file tree.
 *
 * Folders are normally *derived* from file names (`caps/refunds.yaml` implies a
 * `caps/` folder), so an empty folder has nothing to derive it from. Rather than
 * a server-side folder entity, we remember empty folders in `localStorage`,
 * keyed by project: a folder the user creates survives a reload on the same
 * device. The moment a real file lands under the prefix the folder is derived
 * anyway, so we drop it from the cache (reconciled against the file list).
 *
 * This is a per-device convenience, never authoritative state — every read is
 * guarded, and the tree renders correctly if storage is empty or unavailable.
 */

import { useCallback, useEffect, useState } from "react";

import type { PolicyFileRead } from "./api";

const key = (projectId: string) => `hexgate-empty-folders:${projectId}`;

function read(projectId: string): string[] {
  try {
    const raw = localStorage.getItem(key(projectId));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((p) => typeof p === "string")
      : [];
  } catch {
    return [];
  }
}

function write(projectId: string, prefixes: string[]): void {
  try {
    localStorage.setItem(key(projectId), JSON.stringify(prefixes));
  } catch {
    // Private mode / blocked storage — the folder just won't persist.
  }
}

/** A prefix is still empty when no file lives at or under it. */
function isEmpty(prefix: string, files: PolicyFileRead[]): boolean {
  return !files.some(
    (f) => f.name === prefix || f.name.startsWith(`${prefix}/`),
  );
}

export interface EmptyFolders {
  /** Prefixes with no file under them yet — injected into the tree as folders. */
  folders: string[];
  addFolder: (prefix: string) => void;
  removeFolder: (prefix: string) => void;
}

/**
 * The project's remembered empty folders, reconciled against `files`: a prefix
 * that has since been filled drops out (it's a derived folder now), and the
 * pruned list is written back so the cache never accumulates stale entries.
 */
export function useEmptyFolders(
  projectId: string | null,
  files: PolicyFileRead[],
): EmptyFolders {
  const [folders, setFolders] = useState<string[]>(() =>
    projectId ? read(projectId).filter((p) => isEmpty(p, files)) : [],
  );
  // Reconcile during render (not in an effect — that cascades renders) when the
  // project or the file set changes. `filesKey` is content-based, not the array
  // identity, so a caller passing a fresh `[]` each render doesn't loop.
  const filesKey = files.map((f) => f.name).join("\n");
  const [scoped, setScoped] = useState(projectId);
  const [seenKey, setSeenKey] = useState(filesKey);

  if (projectId !== scoped) {
    // Project switched: load that project's remembered folders, reconciled
    // against its current files.
    setScoped(projectId);
    setSeenKey(filesKey);
    const loaded = projectId ? read(projectId) : [];
    setFolders(loaded.filter((p) => isEmpty(p, files)));
  } else if (filesKey !== seenKey) {
    // Files changed: drop any prefix a file now lives under. Keep the array
    // identity stable when nothing changed so this doesn't loop.
    setSeenKey(filesKey);
    setFolders((prev) => {
      const kept = prev.filter((p) => isEmpty(p, files));
      return kept.length === prev.length ? prev : kept;
    });
  }

  // Persist the remembered set (an external-system sync, so an effect fits).
  useEffect(() => {
    if (projectId) write(projectId, folders);
  }, [projectId, folders]);

  const addFolder = useCallback(
    (prefix: string) => {
      const clean = prefix.replace(/^\/+|\/+$/g, "").trim();
      // Ignore a blank name, or a prefix a file already lives under — that's a
      // derived folder, so remembering it would just be stale cache.
      if (!clean || !isEmpty(clean, files)) return;
      setFolders((prev) => (prev.includes(clean) ? prev : [...prev, clean]));
    },
    [files],
  );

  // Remove the folder and any empty subfolders under it.
  const removeFolder = useCallback((prefix: string) => {
    setFolders((prev) =>
      prev.filter((p) => p !== prefix && !p.startsWith(`${prefix}/`)),
    );
  }, []);

  return { folders, addFolder, removeFolder };
}
