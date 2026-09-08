import { useState } from "react";
import { toast } from "sonner";
import {
  ChevronDown,
  ChevronRight,
  FilePlus,
  FileText,
  Trash2,
} from "lucide-react";

import { ApiError, type PolicyFileRead } from "@/lib/api";
import { useDeleteFile } from "@/lib/policy_files";
import { buildFileTree, ENTRY_FILE, type TreeNode } from "@/lib/file_tree";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The compose file tree. Renders the flat `policy_file` list as a nested
 * filesystem view (folders derived from `/`-separated names), with the entry
 * `policy.yaml` pinned first. New-file adds an in-memory buffer (no server row
 * until first save); delete removes a stored file (409 if still imported).
 */
export function FileTree({
  files,
  selected,
  onSelect,
  onNewFile,
  onDeleted,
  projectId,
  canManage,
  dirtyKeys,
}: {
  files: PolicyFileRead[];
  selected: string | null;
  onSelect: (name: string) => void;
  onNewFile: (name: string) => void;
  /** Notify the page a stored file is gone, so it can drop any open tab/buffer
   * for it (else a phantom tab's Save would re-create the file). */
  onDeleted?: (name: string) => void;
  projectId: string;
  canManage: boolean;
  dirtyKeys: Set<string>;
}) {
  const tree = buildFileTree(files);
  const del = useDeleteFile(projectId);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");

  const known = new Set(files.map((f) => f.name));

  function submitNew() {
    const name = newName.trim();
    setAdding(false);
    setNewName("");
    if (!name) return;
    if (known.has(name)) {
      onSelect(name); // already exists — just open it
      return;
    }
    onNewFile(name);
  }

  function remove(name: string) {
    del.mutate(name, {
      onError: (e) =>
        toast.error(
          e instanceof ApiError ? e.message : `Could not delete ${name}`,
        ),
      onSuccess: () => {
        onDeleted?.(name);
        toast.success(`Deleted ${name}`);
      },
    });
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="text-xs font-medium text-muted-foreground">Files</span>
        {canManage && (
          <Button
            size="icon"
            variant="ghost"
            className="size-6"
            title="New file"
            onClick={() => setAdding(true)}
          >
            <FilePlus className="size-3.5" />
          </Button>
        )}
      </div>
      <div className="flex-1 overflow-y-auto scrollbar-thin py-1">
        {adding && (
          <div className="px-2 py-1">
            <input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onBlur={submitNew}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitNew();
                if (e.key === "Escape") {
                  setAdding(false);
                  setNewName("");
                }
              }}
              placeholder={`e.g. ${ENTRY_FILE}`}
              className="w-full rounded border border-border bg-background px-1.5 py-1 font-mono text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
        )}
        {files.length === 0 && !adding ? (
          <p className="px-3 py-2 text-xs text-muted-foreground">
            No files yet. Add a <span className="font-mono">{ENTRY_FILE}</span>{" "}
            to start.
          </p>
        ) : (
          tree.map((node) => (
            <TreeRow
              key={node.type === "file" ? node.full : node.prefix}
              node={node}
              depth={0}
              selected={selected}
              onSelect={onSelect}
              onDelete={canManage ? remove : undefined}
              dirtyKeys={dirtyKeys}
            />
          ))
        )}
      </div>
    </div>
  );
}

function TreeRow({
  node,
  depth,
  selected,
  onSelect,
  onDelete,
  dirtyKeys,
}: {
  node: TreeNode;
  depth: number;
  selected: string | null;
  onSelect: (name: string) => void;
  onDelete?: (name: string) => void;
  dirtyKeys: Set<string>;
}) {
  const [open, setOpen] = useState(true);
  const pad = { paddingLeft: `${depth * 12 + 8}px` };

  if (node.type === "folder") {
    return (
      <div>
        <button
          onClick={() => setOpen((o) => !o)}
          style={pad}
          className="flex w-full items-center gap-1 py-1 pr-2 text-xs text-muted-foreground hover:text-foreground"
        >
          {open ? (
            <ChevronDown className="size-3 shrink-0" />
          ) : (
            <ChevronRight className="size-3 shrink-0" />
          )}
          <span className="truncate font-medium">{node.name}</span>
        </button>
        {open &&
          node.children.map((child) => (
            <TreeRow
              key={child.type === "file" ? child.full : child.prefix}
              node={child}
              depth={depth + 1}
              selected={selected}
              onSelect={onSelect}
              onDelete={onDelete}
              dirtyKeys={dirtyKeys}
            />
          ))}
      </div>
    );
  }

  const isActive = selected === node.full;
  const isDirty = dirtyKeys.has(node.full);
  const isEntry = node.full === ENTRY_FILE;
  return (
    <div
      onClick={() => onSelect(node.full)}
      style={pad}
      className={cn(
        "group flex cursor-pointer items-center gap-1.5 py-1 pr-2 text-xs",
        isActive
          ? "bg-primary/10 text-primary"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      <FileText className="size-3 shrink-0" />
      <span className={cn("truncate font-mono", isEntry && "font-medium")}>
        {node.name}
      </span>
      {isDirty && (
        <span
          className="size-1.5 shrink-0 rounded-full bg-primary"
          title="Unsaved changes"
        />
      )}
      {onDelete && !isEntry && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onDelete(node.full);
          }}
          title={`Delete ${node.full}`}
          className="ml-auto shrink-0 rounded p-0.5 opacity-0 hover:bg-accent group-hover:opacity-100"
        >
          <Trash2 className="size-3" />
        </button>
      )}
    </div>
  );
}
