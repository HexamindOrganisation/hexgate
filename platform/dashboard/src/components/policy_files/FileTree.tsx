import { useRef, useState } from "react";
import { toast } from "sonner";
import {
  ChevronDown,
  ChevronRight,
  FilePlus,
  FileText,
  FolderPlus,
  Pencil,
  Trash2,
} from "lucide-react";

import { ApiError, type PolicyFileRead } from "@/lib/api";
import { useDeleteFile } from "@/lib/policy_files";
import {
  baseName,
  buildFileTree,
  ENTRY_FILE,
  folderHasFiles,
  type TreeNode,
} from "@/lib/file_tree";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The compose file tree. Renders the flat `policy_file` list as a nested
 * filesystem view (folders derived from `/`-separated names, plus any remembered
 * empty folders), with the entry `policy.yaml` pinned first and badged. New-file
 * adds an in-memory buffer (no server row until first save); files rename/move
 * in place (drag onto a folder, or the pencil) and delete removes a stored file
 * (409 if still imported). Empty folders are a per-device convenience.
 */
export function FileTree({
  files,
  emptyFolders,
  selected,
  onSelect,
  onNewFile,
  onDeleted,
  onAddFolder,
  onRemoveFolder,
  onRename,
  projectId,
  canManage,
  dirtyKeys,
}: {
  files: PolicyFileRead[];
  emptyFolders: string[];
  selected: string | null;
  onSelect: (name: string) => void;
  onNewFile: (name: string) => void;
  /** Notify the page a stored file is gone, so it can drop any open tab/buffer
   * for it (else a phantom tab's Save would re-create the file). */
  onDeleted?: (name: string) => void;
  onAddFolder: (prefix: string) => void;
  onRemoveFolder: (prefix: string) => void;
  /** Rename or move a stored file. Move = a rename with a new path prefix. */
  onRename: (oldName: string, newName: string) => void;
  projectId: string;
  canManage: boolean;
  dirtyKeys: Set<string>;
}) {
  const tree = buildFileTree(files, emptyFolders);
  const del = useDeleteFile(projectId);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");
  const [addingFolder, setAddingFolder] = useState(false);
  const [folderName, setFolderName] = useState("");

  const known = new Set(files.map((f) => f.name));

  function startAdd(prefix = "") {
    setAddingFolder(false);
    setNewName(prefix ? `${prefix}/` : "");
    setAdding(true);
  }

  // Open the new-folder input, pre-filled with `prefix/` so the folder is
  // created under `prefix` (empty prefix → a top-level folder).
  function startAddFolder(prefix = "") {
    setAdding(false);
    setFolderName(prefix ? `${prefix}/` : "");
    setAddingFolder(true);
  }

  function submitNew() {
    const name = newName.trim();
    setAdding(false);
    setNewName("");
    if (!name || name.endsWith("/")) return;
    if (known.has(name)) {
      onSelect(name); // already exists — just open it
      return;
    }
    onNewFile(name);
  }

  function submitFolder() {
    const prefix = folderName.trim();
    setAddingFolder(false);
    setFolderName("");
    // onAddFolder (useEmptyFolders) normalizes slashes and rejects non-empty
    // prefixes, so just hand it the raw name.
    if (prefix) onAddFolder(prefix);
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

  // Drag a leaf onto a folder (or the root) to move it there. A no-op when the
  // parent folder is unchanged; the entry file and dirty files don't drag.
  function move(oldName: string, destPrefix: string) {
    const leaf = baseName(oldName);
    const newName = destPrefix ? `${destPrefix}/${leaf}` : leaf;
    if (newName !== oldName) onRename(oldName, newName);
  }

  const actions: TreeActions = {
    selected,
    onSelect,
    onDelete: canManage ? remove : undefined,
    onNewInFolder: canManage ? startAdd : undefined,
    onNewFolderInFolder: canManage ? startAddFolder : undefined,
    onRemoveFolder: canManage ? onRemoveFolder : undefined,
    onRename: canManage ? onRename : undefined,
    onMove: canManage ? move : undefined,
    dirtyKeys,
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-xs font-medium text-muted-foreground">Files</span>
        {canManage && (
          <div className="flex items-center gap-0.5">
            <Button
              size="icon"
              variant="ghost"
              className="size-6"
              title="New folder"
              onClick={() => startAddFolder()}
            >
              <FolderPlus className="size-3.5" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              className="size-6"
              title="New file"
              onClick={() => startAdd()}
            >
              <FilePlus className="size-3.5" />
            </Button>
          </div>
        )}
      </div>
      <div
        className="scrollbar-thin flex-1 overflow-y-auto py-1"
        // Dropping in the empty area moves a leaf to the project root.
        onDragOver={(e) => canManage && e.preventDefault()}
        onDrop={(e) => {
          const name = e.dataTransfer.getData("text/plain");
          if (canManage && name) move(name, "");
        }}
      >
        {addingFolder && (
          <div className="px-2 py-1">
            <input
              autoFocus
              value={folderName}
              onChange={(e) => setFolderName(e.target.value)}
              onBlur={submitFolder}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitFolder();
                if (e.key === "Escape") {
                  setAddingFolder(false);
                  setFolderName("");
                }
              }}
              placeholder="e.g. caps"
              aria-label="New folder name"
              className="w-full rounded border border-border bg-background px-1.5 py-1 font-mono text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
        )}
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
              aria-label="New file name"
              className="w-full rounded border border-border bg-background px-1.5 py-1 font-mono text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
        )}
        {files.length === 0 && emptyFolders.length === 0 && !adding ? (
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
              actions={actions}
            />
          ))
        )}
      </div>
    </div>
  );
}

/** The per-row callbacks + selection, bundled to avoid threading a dozen props
 * through the recursive rows. Absent callbacks (no manage rights) hide their
 * affordance. */
interface TreeActions {
  selected: string | null;
  onSelect: (name: string) => void;
  onDelete?: (name: string) => void;
  onNewInFolder?: (prefix: string) => void;
  onNewFolderInFolder?: (prefix: string) => void;
  onRemoveFolder?: (prefix: string) => void;
  onRename?: (oldName: string, newName: string) => void;
  onMove?: (oldName: string, destPrefix: string) => void;
  dirtyKeys: Set<string>;
}

function TreeRow({
  node,
  depth,
  actions,
}: {
  node: TreeNode;
  depth: number;
  actions: TreeActions;
}) {
  const [open, setOpen] = useState(true);
  const [over, setOver] = useState(false);
  const pad = { paddingLeft: `${depth * 12 + 8}px` };

  if (node.type === "folder") {
    const empty = !folderHasFiles(node);
    return (
      <div>
        <div
          style={pad}
          onDragOver={(e) => {
            if (!actions.onMove) return;
            e.preventDefault();
            e.stopPropagation();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.stopPropagation();
            setOver(false);
            const name = e.dataTransfer.getData("text/plain");
            if (name) actions.onMove?.(name, node.prefix);
          }}
          className={cn(
            "group flex items-center gap-1 py-1 pr-2 text-xs text-muted-foreground hover:text-foreground",
            over && "bg-primary/10 text-primary",
          )}
        >
          <button
            onClick={() => setOpen((o) => !o)}
            className="flex min-w-0 flex-1 items-center gap-1"
          >
            {open ? (
              <ChevronDown className="size-3 shrink-0" />
            ) : (
              <ChevronRight className="size-3 shrink-0" />
            )}
            <span className="truncate font-medium">{node.name}</span>
          </button>
          {actions.onNewFolderInFolder && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                actions.onNewFolderInFolder?.(node.prefix);
              }}
              title={`New folder in ${node.prefix}`}
              className="shrink-0 rounded p-0.5 opacity-0 hover:bg-accent group-hover:opacity-100"
            >
              <FolderPlus className="size-3" />
            </button>
          )}
          {actions.onNewInFolder && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                actions.onNewInFolder?.(node.prefix);
              }}
              title={`New file in ${node.prefix}`}
              className="shrink-0 rounded p-0.5 opacity-0 hover:bg-accent group-hover:opacity-100"
            >
              <FilePlus className="size-3" />
            </button>
          )}
          {empty && actions.onRemoveFolder && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                actions.onRemoveFolder?.(node.prefix);
              }}
              title={`Remove empty folder ${node.prefix}`}
              className="shrink-0 rounded p-0.5 opacity-0 hover:bg-accent group-hover:opacity-100"
            >
              <Trash2 className="size-3" />
            </button>
          )}
        </div>
        {open &&
          node.children.map((child) => (
            <TreeRow
              key={child.type === "file" ? child.full : child.prefix}
              node={child}
              depth={depth + 1}
              actions={actions}
            />
          ))}
      </div>
    );
  }

  return <FileRow node={node} pad={pad} actions={actions} />;
}

function FileRow({
  node,
  pad,
  actions,
}: {
  node: Extract<TreeNode, { type: "file" }>;
  pad: { paddingLeft: string };
  actions: TreeActions;
}) {
  const [renaming, setRenaming] = useState(false);
  const [value, setValue] = useState(node.full);
  // Enter commits then unmounts the input, whose blur would fire submitRename a
  // second time — guard so the rename dispatches exactly once per edit.
  const committed = useRef(false);

  const isActive = actions.selected === node.full;
  const isDirty = actions.dirtyKeys.has(node.full);
  const isEntry = node.full === ENTRY_FILE;
  // The resolver starts from the entry file, and a rename can't safely rewrite
  // content that isn't saved yet — so both are barred from rename/move.
  const canRename = !!actions.onRename && !isEntry && !isDirty;

  function startRename() {
    committed.current = false;
    setValue(node.full);
    setRenaming(true);
  }

  function submitRename() {
    if (committed.current) return;
    committed.current = true;
    const next = value.trim();
    setRenaming(false);
    if (next && next !== node.full && !next.endsWith("/")) {
      actions.onRename?.(node.full, next);
    } else {
      setValue(node.full);
    }
  }

  if (renaming) {
    return (
      <div style={pad} className="px-0 py-1 pr-2">
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={submitRename}
          onFocus={(e) => e.target.select()}
          onKeyDown={(e) => {
            if (e.key === "Enter") submitRename();
            if (e.key === "Escape") {
              setValue(node.full);
              setRenaming(false);
            }
          }}
          aria-label={`Rename ${node.full}`}
          className="w-full rounded border border-border bg-background px-1.5 py-0.5 font-mono text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
      </div>
    );
  }

  return (
    <div
      onClick={() => actions.onSelect(node.full)}
      draggable={canRename}
      onDragStart={(e) => e.dataTransfer.setData("text/plain", node.full)}
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
      {isEntry && (
        <span
          className="shrink-0 rounded bg-primary/10 px-1 text-[10px] font-medium text-primary"
          title="The file the resolver starts from"
        >
          entry
        </span>
      )}
      {isDirty && (
        <span
          className="size-1.5 shrink-0 rounded-full bg-primary"
          title="Unsaved changes"
        />
      )}
      <div className="ml-auto flex shrink-0 items-center">
        {canRename && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              startRename();
            }}
            title={`Rename ${node.full}`}
            className="rounded p-0.5 opacity-0 hover:bg-accent group-hover:opacity-100"
          >
            <Pencil className="size-3" />
          </button>
        )}
        {actions.onDelete && !isEntry && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              actions.onDelete?.(node.full);
            }}
            title={`Delete ${node.full}`}
            className="rounded p-0.5 opacity-0 hover:bg-accent group-hover:opacity-100"
          >
            <Trash2 className="size-3" />
          </button>
        )}
      </div>
    </div>
  );
}
