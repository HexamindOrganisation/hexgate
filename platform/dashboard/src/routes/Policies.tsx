import { useCallback, useMemo, useState } from "react";
import { FileText, X } from "lucide-react";

import type { PolicyFileDraft } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import {
  useCanManagePolicy,
  useDebouncedValue,
  usePolicyCheck,
  usePolicyFiles,
  usePolicyPreview,
  useResolvedPolicy,
} from "@/lib/policy_files";
import { baseName, ENTRY_FILE } from "@/lib/file_tree";
import { cn } from "@/lib/utils";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { DocsLink } from "@/components/DocsLink";
import { DOC_PATHS } from "@/lib/docs";
import { ModularBanner } from "@/components/policy_files/ModularBanner";
import { FileTree } from "@/components/policy_files/FileTree";
import { EditorPane } from "@/components/policy_files/EditorPane";
import { InspectorTabs } from "@/components/policy_files/InspectorTabs";

const MAX_TABS = 6;

/**
 * Compose policy editor. Three panes: the file tree (the project's
 * `policy_file` rows as a filesystem), a CodeMirror editor for the open file,
 * and an inspector showing the composed policy per role, lints, a decision
 * tester, and the policy graph. The resolved + lints panes reflect the unsaved
 * edit live (debounced `POST /policy/preview`); Save is explicit.
 */
export function PoliciesPage() {
  const scope = useProjectScoped();
  const projectId = scope.projectId;
  const canManage = useCanManagePolicy();

  const filesQuery = usePolicyFiles(projectId);
  const files = useMemo(() => filesQuery.data ?? [], [filesQuery.data]);
  const modular = files.some((f) => f.name === ENTRY_FILE);

  // The open file (its name), the open tabs, and the per-file unsaved buffers.
  // A tab may name a not-yet-saved file (an "untitled" buffer) — it lives only
  // in `openTabs`/`drafts` until the first Save persists it into `files`.
  const [selection, setSelection] = useState<string | null>(null);
  const [openTabs, setOpenTabs] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const dirtyKeys = useMemo(() => new Set(Object.keys(drafts)), [drafts]);

  // Default the open file once files load: the entry file, else the first one.
  const active =
    selection ??
    (files.some((f) => f.name === ENTRY_FILE)
      ? ENTRY_FILE
      : (files[0]?.name ?? null));
  const activeTabs =
    openTabs.length || active === null ? openTabs : [active as string];

  const openFile = useCallback((name: string) => {
    setSelection(name);
    setOpenTabs((prev) =>
      [...prev.filter((t) => t !== name), name].slice(-MAX_TABS),
    );
  }, []);

  const closeTab = useCallback(
    (name: string) => {
      // Compute the next tabs and the neighbor to select in the handler — never
      // call setSelection from inside the setOpenTabs updater (impure; React
      // StrictMode double-invokes updaters).
      const idx = openTabs.indexOf(name);
      const next = openTabs.filter((t) => t !== name);
      setOpenTabs(next);
      setSelection((sel) =>
        sel === name ? (next[Math.min(idx, next.length - 1)] ?? null) : sel,
      );
    },
    [openTabs],
  );

  // A deleted file must leave no phantom tab/selection/draft — otherwise its
  // stale buffer could be Saved and re-create the file.
  const onFileDeleted = useCallback((name: string) => {
    setOpenTabs((prev) => prev.filter((t) => t !== name));
    setSelection((sel) => (sel === name ? null : sel));
    setDrafts((prev) => {
      if (!(name in prev)) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });
  }, []);

  const onNewFile = useCallback(
    (name: string) => {
      setDrafts((prev) => ({ ...prev, [name]: prev[name] ?? "" }));
      openFile(name);
    },
    [openFile],
  );

  const onPersist = useCallback((key: string, text: string, dirty: boolean) => {
    setDrafts((prev) => {
      if (dirty) return { ...prev, [key]: text };
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  }, []);

  // The editor's unsaved overlay (null = clean or unparseable) + whether the
  // buffer parses client-side (gates the server preview round-trip).
  const [draft, setDraft] = useState<PolicyFileDraft | null>(null);
  const [draftParses, setDraftParses] = useState(true);
  const onDraftChange = useCallback(
    (next: PolicyFileDraft | null, parses: boolean) => {
      setDraft(next);
      setDraftParses(parses);
    },
    [],
  );

  // Which executing agent's column the Resolved tab + preview inspect. "*" is
  // the generic view; a named agent shows its own composed policy.
  const [inspectAgent, setInspectAgent] = useState<string>("*");

  // A project switch must not carry another project's tabs/edits over — else a
  // Save could write project A's buffer into B. Reset all local editor state via
  // the render-phase "adjust state when a prop changes" pattern (no effect, so
  // the reset lands in the same render as the switch — no stale-tab flash).
  const [scopedProject, setScopedProject] = useState(projectId);
  if (projectId !== scopedProject) {
    setScopedProject(projectId);
    setSelection(null);
    setOpenTabs([]);
    setDrafts({});
    setInspectAgent("*");
    setDraft(null);
    setDraftParses(true);
  }

  const debouncedDraft = useDebouncedValue(draft, 600);
  const draftActive = draft !== null;
  const previewEnabled = draftParses && debouncedDraft !== null;
  const preview = usePolicyPreview(
    projectId,
    debouncedDraft,
    inspectAgent,
    previewEnabled,
  );
  // Only a compose project has a composed policy to resolve/lint — skip the
  // round-trips (and resolve's 422) on classic projects.
  const storedResolve = useResolvedPolicy(
    projectId,
    undefined,
    inspectAgent,
    modular,
  );
  const check = usePolicyCheck(projectId, modular);

  const usePreviewData = draftActive && !!preview.data;
  const resolved = usePreviewData ? preview.data?.resolved : storedResolve.data;
  const lints = useMemo(
    () =>
      usePreviewData ? (preview.data?.lints ?? []) : (check.data?.lints ?? []),
    [usePreviewData, preview.data, check.data],
  );
  const resolves = lints.every((l) => l.severity !== "error");

  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="policies" />;
  }

  const loading = filesQuery.isLoading;

  return (
    // Recessed page ground with a floating, softly-rounded editor card. Panes
    // are separated by background shade, not hard border lines (the VSCode
    // "shades, not rules" feel).
    <div className="-mx-8 -my-6 h-screen overflow-hidden bg-muted/20 p-3">
      <div className="flex h-full flex-col overflow-hidden rounded-xl border border-border/60 bg-card shadow-sm">
        {!loading && (
          <ModularBanner
            modular={modular}
            trailing={
              <DocsLink path={DOC_PATHS.policies} label="Policy docs" />
            }
          />
        )}

        {loading || !projectId ? (
          <div className="flex-1 grid place-items-center text-sm text-muted-foreground">
            {scope.status === "loading" || loading
              ? "Loading policy…"
              : "No project selected."}
          </div>
        ) : (
          <div className="flex-1 grid grid-cols-[240px_minmax(0,1fr)_minmax(320px,380px)] overflow-hidden">
            <div className="overflow-hidden bg-background/40">
              <FileTree
                files={files}
                selected={active}
                onSelect={openFile}
                onNewFile={onNewFile}
                onDeleted={onFileDeleted}
                projectId={projectId}
                canManage={canManage}
                dirtyKeys={dirtyKeys}
              />
            </div>
            <div className="flex flex-col overflow-hidden">
              <div className="flex shrink-0 items-stretch overflow-x-auto border-b border-border bg-background/40 scrollbar-thin">
                {activeTabs.map((tab) => {
                  const isActive = active === tab;
                  const isDirty = dirtyKeys.has(tab);
                  return (
                    <div
                      key={tab}
                      onClick={() => setSelection(tab)}
                      title={tab}
                      className={cn(
                        "group flex max-w-[170px] min-w-0 cursor-pointer items-center gap-1.5 border-r border-border px-3 py-1.5 text-xs",
                        isActive
                          ? "bg-card text-foreground"
                          : "text-muted-foreground hover:text-foreground",
                      )}
                    >
                      <FileText className="size-3 shrink-0 text-muted-foreground" />
                      <span className="truncate font-mono">
                        {baseName(tab)}
                      </span>
                      {isDirty && (
                        <span
                          className="size-1.5 shrink-0 rounded-full bg-primary group-hover:hidden"
                          title="Unsaved changes"
                        />
                      )}
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          closeTab(tab);
                        }}
                        title="Close tab"
                        className={cn(
                          "shrink-0 rounded p-0.5 hover:bg-accent",
                          isDirty
                            ? "hidden group-hover:block"
                            : "opacity-0 group-hover:opacity-100",
                        )}
                      >
                        <X className="size-3" />
                      </button>
                    </div>
                  );
                })}
              </div>
              <div className="flex-1 overflow-hidden">
                {active === null ? (
                  <div className="h-full grid place-items-center px-6 text-center">
                    <p className="text-xs text-muted-foreground">
                      No file open. Add a{" "}
                      <span className="font-mono">{ENTRY_FILE}</span> to start
                      the compose policy.
                    </p>
                  </div>
                ) : (
                  <EditorPane
                    projectId={projectId}
                    name={active}
                    files={files}
                    lints={lints}
                    canManage={canManage}
                    onDraftChange={onDraftChange}
                    drafts={drafts}
                    onPersist={onPersist}
                  />
                )}
              </div>
            </div>
            <div className="overflow-hidden bg-background/40">
              <InspectorTabs
                projectId={projectId}
                resolved={resolved}
                lints={lints}
                draft={draftActive ? draft : null}
                resolves={resolves}
                modular={modular}
                previewing={draftActive && preview.isFetching}
                inspectAgent={inspectAgent}
                onInspectAgentChange={setInspectAgent}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
