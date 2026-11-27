import { Hexagon, X } from "lucide-react";

import type { AssistantChange } from "@/lib/assistant";

/**
 * The editor's note that the open file holds what the assistant's latest
 * change to it wrote, with that change's summary. Shown only while the stored
 * text is still the change's (its hash) and the buffer has no unsaved edits;
 * a buffer with edits keeps them, and says nothing here.
 */
export function AssistantChangeBar({
  last,
  file,
  storedHash,
  dirty,
  closed,
  onDismiss,
}: {
  /** The assistant's latest change to the file and the hash it left. */
  last: { change: AssistantChange; afterHash: string } | null;
  /** The open file's name. */
  file: string;
  /** The open file's stored `text_hash`; undefined while it is unsaved. */
  storedHash: string | undefined;
  dirty: boolean;
  /** The bars the user closed, as "change_id:file". */
  closed: ReadonlySet<string>;
  onDismiss: (key: string) => void;
}) {
  if (!last || dirty || storedHash !== last.afterHash) return null;
  const key = `${last.change.change_id}:${file}`;
  if (closed.has(key)) return null;
  return (
    <div className="flex shrink-0 items-center gap-2 border-y border-border/60 bg-action/10 px-3 py-1 text-[11px]">
      <Hexagon className="size-3 shrink-0 text-action" />
      <span className="shrink-0 font-medium">Changed by the assistant</span>
      <span
        className="truncate text-muted-foreground"
        title={last.change.summary}
      >
        {last.change.summary}
      </span>
      <button
        type="button"
        onClick={() => onDismiss(key)}
        title="Dismiss"
        aria-label="Dismiss"
        className="ml-auto shrink-0 rounded p-0.5 hover:bg-accent"
      >
        <X className="size-3" />
      </button>
    </div>
  );
}
