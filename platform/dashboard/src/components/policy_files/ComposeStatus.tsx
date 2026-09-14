import type { ReactNode } from "react";
import { Info, Layers } from "lucide-react";

import { ENTRY_FILE } from "@/lib/file_tree";

/**
 * Classic-project explainer — the top CTA a project shows until it has an entry
 * `policy.yaml`. It's the only cue that the file library is inert and that
 * agents still enforce their own policy, so it stays prominent. `trailing`
 * holds right-aligned actions (the docs link).
 */
export function ClassicProjectBanner({ trailing }: { trailing?: ReactNode }) {
  return (
    <div className="flex items-center gap-2 border-b border-approval/30 bg-approval/5 px-6 py-2 text-xs text-approval">
      <Info className="size-3.5 shrink-0" />
      <span className="min-w-0 flex-1">
        Classic project — agents enforce their own policy. Add a{" "}
        <span className="font-mono">{ENTRY_FILE}</span> to switch this project
        to the composed file library.
      </span>
      {trailing && <div className="shrink-0">{trailing}</div>}
    </div>
  );
}

/**
 * Compose-active status bar — a quiet footer confirming the composed bundle is
 * live (the "it's enforced" signal, minus the paragraph the old banner spent on
 * it). Sits below the panes as an ambient status strip, not a message.
 */
export function ComposeStatusBar({ trailing }: { trailing?: ReactNode }) {
  return (
    <div className="flex items-center justify-between border-t border-border px-4 py-1 text-[11px] text-muted-foreground">
      <span className="inline-flex items-center gap-1.5">
        <Layers className="size-3 text-muted-foreground" />
        <span className="text-foreground/70">Compose</span>
        <span aria-hidden>·</span>
        <span className="font-medium text-allow">active</span>
      </span>
      {trailing}
    </div>
  );
}
