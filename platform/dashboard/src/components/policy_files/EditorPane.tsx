import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { load, YAMLException } from "js-yaml";
import { FileText, RotateCcw, Save } from "lucide-react";

import {
  ApiError,
  type PolicyFileDraft,
  type PolicyFileRead,
  type PolicyLint,
  type PolicyValidationError,
} from "@/lib/api";
import { useUpsertFile } from "@/lib/policy_files";
import { baseName, lintToLine } from "@/lib/file_tree";
import { PolicyEditor } from "@/components/PolicyEditor";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ClientParse {
  ok: boolean;
  /** 1-based line of the parse error, when the exception carries a mark. */
  error: { line: number | null; message: string } | null;
}

function clientParse(text: string): ClientParse {
  try {
    load(text);
    return { ok: true, error: null };
  } catch (e) {
    if (e instanceof YAMLException) {
      const line = e.mark ? e.mark.line + 1 : null;
      return { ok: false, error: { line, message: e.reason || e.message } };
    }
    return { ok: false, error: { line: null, message: String(e) } };
  }
}

export function EditorPane({
  projectId,
  name,
  files,
  lints,
  canManage,
  onDraftChange,
  drafts,
  onPersist,
}: {
  projectId: string;
  /** The open file's full name (its key). */
  name: string;
  files: PolicyFileRead[];
  lints: PolicyLint[];
  canManage: boolean;
  onDraftChange: (draft: PolicyFileDraft | null, parses: boolean) => void;
  // Per-file unsaved buffers (keyed by file name), lifted to the page so edits
  // survive switching files. `onPersist` stores/clears an entry as the buffer
  // changes; `dirty` is whether it differs from the saved text.
  drafts: Record<string, string>;
  onPersist: (key: string, text: string, dirty: boolean) => void;
}) {
  const upsert = useUpsertFile(projectId);

  // Authoritative stored text for the open file ("" for a not-yet-saved one).
  const saved = useMemo(
    () => files.find((f) => f.name === name)?.content ?? "",
    [files, name],
  );

  const [draft, setDraft] = useState(() => drafts[name] ?? saved);
  // The server's message from the last failed Save (422 invalid / 409 breaks
  // resolution), shown inline until the buffer changes or a Save succeeds.
  const [saveError, setSaveError] = useState<string | null>(null);

  // Re-sync the buffer via the render-phase "adjust state when a prop changes"
  // pattern (no effect, so the reset lands in the same render as the switch).
  // On a file switch, load the newly-selected file's persisted buffer or its
  // stored text. On a background refetch of the SAME file, only adopt the new
  // server copy when the buffer is clean — never clobber unsaved edits.
  const [syncKey, setSyncKey] = useState(name);
  const [syncSaved, setSyncSaved] = useState(saved);
  if (name !== syncKey) {
    setSyncKey(name);
    setSyncSaved(saved);
    setDraft(drafts[name] ?? saved);
    setSaveError(null);
  } else if (saved !== syncSaved) {
    const wasClean = draft === syncSaved;
    setSyncSaved(saved);
    if (wasClean) setDraft(saved);
  }

  // A not-yet-saved file (a "new" tab, absent from `files`) is always dirty —
  // it needs a Save to exist at all — so an intentionally-empty new file can be
  // created and its Save/badge stay consistent with the tree's unsaved dot.
  const isNew = useMemo(
    () => !files.some((f) => f.name === name),
    [files, name],
  );
  const dirty = isNew || draft !== saved;
  const parse = useMemo(() => clientParse(draft), [draft]);

  // Publish the live overlay up so the inspector previews the unsaved edit.
  // Clean or unparseable → no overlay (the inspector shows stored state; the
  // parse error renders inline here).
  useEffect(() => {
    if (!dirty || !parse.ok) {
      onDraftChange(null, parse.ok);
      return;
    }
    onDraftChange({ name, content: draft }, true);
  }, [draft, dirty, parse.ok, name, onDraftChange]);

  const handleChange = (next: string) => {
    setDraft(next);
    setSaveError(null);
    onPersist(name, next, next !== saved);
  };

  // Semantic lints for THIS file, anchored to the tool's line. Whole-file and
  // cross-role lints have no line and surface in the inspector's Lints tab.
  const fileLints = useMemo<PolicyLint[]>(
    () => lints.filter((l) => l.source === name),
    [lints, name],
  );

  const diagnostics = useMemo<PolicyValidationError[]>(() => {
    const out: PolicyValidationError[] = [];
    if (parse.error) {
      out.push({
        role: null,
        tool: null,
        line: parse.error.line,
        message: parse.error.message,
      });
    }
    for (const l of fileLints) {
      const line = lintToLine(draft, l);
      if (line)
        out.push({ role: l.role, tool: l.tool, line, message: l.message });
    }
    return out;
  }, [parse.error, fileLints, draft]);

  // Everything to list in the inline banner: the client parse error, the last
  // Save error (422/409), and ALL this-file lints — including semantic ones
  // that can't be pinned to a line, so a file-level error is visible next to
  // the code, not only in the inspector's Lints tab.
  const bannerItems = useMemo(() => {
    const out: { line: number | null; message: string }[] = [];
    if (parse.error) {
      out.push({ line: parse.error.line, message: parse.error.message });
    }
    if (saveError) out.push({ line: null, message: saveError });
    for (const l of fileLints) {
      out.push({ line: lintToLine(draft, l), message: l.message });
    }
    return out;
  }, [parse.error, saveError, fileLints, draft]);

  const hasError =
    !parse.ok || !!saveError || fileLints.some((l) => l.severity === "error");

  function handleSave() {
    upsert.mutate(
      { name, content: draft },
      {
        onSuccess: () => {
          setSaveError(null);
          onPersist(name, draft, false);
          toast.success(`Saved ${name}`);
        },
        onError: (e) => {
          // Surface the server's 422 (invalid) / 409 (breaks resolution)
          // message inline in the diagnostics bar, not just a transient toast.
          const msg = e instanceof ApiError ? e.message : "Could not save";
          setSaveError(msg);
          toast.error(msg);
        },
      },
    );
  }

  const saving = upsert.isPending;

  return (
    <div className="h-full flex flex-col">
      <header className="flex items-center justify-between gap-2 px-4 py-2 border-b border-border">
        <div className="flex items-center gap-2 text-sm min-w-0">
          <FileText className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="font-mono truncate" title={name}>
            {baseName(name)}
          </span>
          {dirty && <Badge variant="approval">unsaved</Badge>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setDraft(saved);
              setSaveError(null);
              onPersist(name, saved, false);
            }}
            disabled={!dirty || saving}
            className="gap-1.5 h-8"
            title="Discard unsaved changes"
          >
            <RotateCcw className="size-3.5" />
            Discard
          </Button>
          {canManage && (
            <Button
              size="sm"
              onClick={handleSave}
              disabled={!dirty || saving}
              className="gap-1.5 h-8"
            >
              <Save className="size-3.5" />
              {saving ? "Saving…" : "Save"}
            </Button>
          )}
        </div>
      </header>
      <PolicyEditor
        value={draft}
        onChange={handleChange}
        diagnostics={diagnostics}
        readOnly={!canManage}
        className="flex-1 overflow-hidden"
      />
      {/* Diagnostics as a bottom status bar (VSCode "problems"), so appearing/
          clearing them never shifts the editor content down from the top. */}
      {bannerItems.length > 0 && (
        <div
          className={cn(
            "shrink-0 max-h-28 overflow-y-auto scrollbar-thin px-4 py-1.5 text-xs border-t font-mono space-y-0.5",
            hasError
              ? "bg-deny/5 border-deny/30 text-deny"
              : "bg-approval/5 border-approval/30 text-approval",
          )}
        >
          {bannerItems.map((d, i) => (
            <div key={i}>
              {d.line ? `L${d.line} — ` : ""}
              {d.message}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
