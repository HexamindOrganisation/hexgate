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
  // Where an inline new-folder row is open: null = none, "" = at the root, a
  // prefix = inside that folder. The row shows up in the tree at that spot.
  const [addFolderPrefix, setAddFolderPrefix] = useState<string | null>(null);
  const [folderDraft, setFolderDraft] = useState("");

  const known = new Set(files.map((f) => f.name));

  function startAdd(prefix = "") {
    setAddFolderPrefix(null);
    setNewName(prefix ? `${prefix}/` : "");
    setAdding(true);
  }

  // Open an inline new-folder row at `prefix` (empty prefix → a top-level
  // folder). The row renders in the tree where the folder will land, and takes
  // just the leaf name.
  function startAddFolder(prefix = "") {
    setAdding(false);
    setFolderDraft("");
    setAddFolderPrefix(prefix);
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
    const leaf = folderDraft.trim().replace(/^\/+|\/+$/g, "");
    const prefix = addFolderPrefix;
    setAddFolderPrefix(null);
    setFolderDraft("");
    // Join the leaf under the target prefix (root when prefix is ""); onAddFolder
    // (useEmptyFolders) normalizes slashes.
    if (leaf && prefix !== null) {
      onAddFolder(prefix ? `${prefix}/${leaf}` : leaf);
    }
  }

  function cancelFolder() {
    setAddFolderPrefix(null);
    setFolderDraft("");
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
    addFolderPrefix,
    folderDraft,
    onFolderDraftChange: setFolderDraft,
    onSubmitFolder: submitFolder,
    onCancelFolder: cancelFolder,
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
              className="w-full rounded border border-border bg-background px-1.5 py-1 text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
          </div>
        )}
        {/* A root-level new-folder row appears at the top of the tree. */}
        {addFolderPrefix === "" && (
          <NewFolderRow
            depth={0}
            value={folderDraft}
            onChange={setFolderDraft}
            onSubmit={submitFolder}
            onCancel={cancelFolder}
          />
        )}
        {files.length === 0 &&
        emptyFolders.length === 0 &&
        !adding &&
        addFolderPrefix === null ? (
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

/** An inline, folder-styled row with an editable name, shown in the tree where a
 * new folder will be created — so making a folder reads as a folder appearing in
 * place, not a detached input bar. Takes just the leaf name. */
function NewFolderRow({
  depth,
  value,
  onChange,
  onSubmit,
  onCancel,
}: {
  depth: number;
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  // Enter/blur both fire, and unmounting on submit/cancel triggers a trailing
  // blur — commit once so the folder isn't created twice (or after Escape).
  const done = useRef(false);
  const submit = () => {
    if (done.current) return;
    done.current = true;
    onSubmit();
  };
  return (
    <div
      style={{ paddingLeft: `${depth * 12 + 8}px` }}
      className="flex items-center gap-1 py-1 pr-2 text-xs text-muted-foreground"
    >
      <ChevronDown className="size-3 shrink-0" />
      <input
        autoFocus
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={submit}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
          if (e.key === "Escape") {
            done.current = true; // suppress the unmount blur
            onCancel();
          }
        }}
        placeholder="folder name"
        aria-label="New folder name"
        className="min-w-0 flex-1 rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      />
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
  // Inline new-folder row: which prefix it's open at (null = closed), its draft
  // value, and the commit/cancel handlers. A folder row whose prefix matches
  // renders the row among its children.
  addFolderPrefix: string | null;
  folderDraft: string;
  onFolderDraftChange: (v: string) => void;
  onSubmitFolder: () => void;
  onCancelFolder: () => void;
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

  // Adding a folder here forces this one open so the new row is visible; persist
  // that open state (adjust-during-render, no effect) so the folder stays
  // expanded after the row commits and the freshly-created child is visible.
  const addingHere =
    node.type === "folder" && actions.addFolderPrefix === node.prefix;
  if (addingHere && !open) setOpen(true);

  if (node.type === "folder") {
    const empty = !folderHasFiles(node);
    const isOpen = open || addingHere;
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
            {isOpen ? (
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
        {isOpen && (
          <>
            {addingHere && (
              <NewFolderRow
                depth={depth + 1}
                value={actions.folderDraft}
                onChange={actions.onFolderDraftChange}
                onSubmit={actions.onSubmitFolder}
                onCancel={actions.onCancelFolder}
              />
            )}
            {node.children.map((child) => (
              <TreeRow
                key={child.type === "file" ? child.full : child.prefix}
                node={child}
                depth={depth + 1}
                actions={actions}
              />
            ))}
          </>
        )}
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
          className="w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-ring"
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
      <span className={cn("truncate", isEntry && "font-medium")}>
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
          className="size-1.5 shrink-0 rounded-full bg-approval"
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
