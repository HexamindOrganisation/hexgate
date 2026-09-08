import type { ReactNode } from "react";
import { Info, Layers } from "lucide-react";

import { cn } from "@/lib/utils";
import { ENTRY_FILE } from "@/lib/file_tree";

/**
 * Classic-vs-compose status bar — doubles as the page's top bar. A project is
 * on the compose model once it has an entry `policy.yaml`; until then agents
 * enforce their own policy and the file library changes nothing. `trailing`
 * holds right-aligned actions (e.g. the docs link).
 */
export function ModularBanner({
  modular,
  trailing,
}: {
  modular: boolean;
  trailing?: ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 px-6 py-2 text-xs border-b",
        modular
          ? "border-primary/30 bg-primary/5 text-primary"
          : "border-approval/30 bg-approval/5 text-approval",
      )}
    >
      {modular ? (
        <Layers className="size-3.5 shrink-0" />
      ) : (
        <Info className="size-3.5 shrink-0" />
      )}
      <span className="min-w-0 flex-1">
        {modular ? (
          <>
            Compose policy is <span className="font-medium">active</span>.
            Agents in this project enforce the composed bundle below.
          </>
        ) : (
          <>
            Classic project — agents enforce their own policy. Add a{" "}
            <span className="font-mono">{ENTRY_FILE}</span> to switch this
            project to the composed file library.
          </>
        )}
      </span>
      {trailing && <div className="shrink-0">{trailing}</div>}
    </div>
  );
}
