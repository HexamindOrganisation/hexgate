/**
 * Which roles one agent's graph is drawn for: a button that opens a dialog
 * with a searchable checklist. Several roles show what a caller holding them
 * all may do (the engine's permissive union); none picked is no role filter.
 */

import { useMemo, useState } from "react";
import { ChevronDown, Search } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import type { RoleOption } from "@/lib/agent-graph-view";
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Opens the role picker; says which roles the graph is drawn for. */
export function RolesButton({
  total,
  picked,
  onClick,
  className,
}: {
  total: number;
  picked: number;
  onClick: () => void;
  className?: string;
}) {
  if (!total) return null;
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      onClick={onClick}
      className={cn("h-7 gap-1.5 px-2 text-[11px]", className)}
    >
      {picked ? `Roles · ${picked} of ${total}` : `Roles · all ${total}`}
      <ChevronDown className="size-3.5 text-muted-foreground" />
    </Button>
  );
}

/** Mounted only while open, so each opening starts from what's applied. */
export function RolePickerDialog({
  options,
  picked,
  onClose,
  onApply,
  title = "Show the graph for these roles",
  description = "A caller can hold several roles. The graph shows what a caller with all the picked roles may do.",
  meta = graphMeta,
  applyLabel = (n) => (n ? `Show ${pluralise(n, "role")}` : "Show all roles"),
}: {
  options: RoleOption[];
  picked: string[];
  onClose: () => void;
  onApply: (roles: string[]) => void;
  title?: string;
  description?: string;
  /** The line beside each role. */
  meta?: (o: RoleOption) => string;
  applyLabel?: (picked: number) => string;
}) {
  const [draft, setDraft] = useState(() => new Set(picked));
  const [query, setQuery] = useState("");
  // Only meaningful when the policy gates running the agent by role.
  const gated = options.some((o) => o.runs !== null);
  const [onlyRunners, setOnlyRunners] = useState(gated);
  const runners = options.filter((o) => o.runs).length;
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options.filter(
      (o) =>
        (!onlyRunners || o.runs !== false) &&
        (!q || o.name.toLowerCase().includes(q)),
    );
  }, [options, onlyRunners, query]);
  const hidden = options.length - shown.length;
  const toggle = (name: string) =>
    setDraft((d) => {
      const next = new Set(d);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="flex max-h-[85vh] max-w-[520px] flex-col gap-0 overflow-hidden p-0">
        <div className="space-y-1.5 px-5 pb-3 pt-5">
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </div>
        <div className="space-y-2.5 px-5 pb-3">
          <label className="flex h-10 items-center gap-2 rounded-lg border border-input px-3 focus-within:ring-2 focus-within:ring-ring">
            <Search className="size-3.5 text-muted-foreground" aria-hidden />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`Search ${pluralise(options.length, "role")}`}
              aria-label="Search roles"
              className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
            />
          </label>
          {gated && (
            <div className="flex flex-wrap gap-2">
              <ChipToggle
                pressed={!onlyRunners}
                onClick={() => setOnlyRunners(false)}
              >
                All roles
              </ChipToggle>
              <ChipToggle
                pressed={onlyRunners}
                onClick={() => setOnlyRunners(true)}
              >
                Only roles that run it · {runners}
              </ChipToggle>
            </div>
          )}
        </div>
        <fieldset className="min-h-0 flex-1 overflow-y-auto border-t border-border">
          <legend className="sr-only">Roles</legend>
          {shown.map((o) => (
            <label
              key={o.name}
              className={cn(
                "flex min-h-11 cursor-pointer items-center gap-3 px-5 py-2",
                draft.has(o.name) && "bg-accent/50",
              )}
            >
              <input
                type="checkbox"
                checked={draft.has(o.name)}
                onChange={() => toggle(o.name)}
                className="size-4 accent-[hsl(var(--primary))]"
              />
              <span className="font-mono text-[13px]">{o.name}</span>
              <span className="ml-auto text-xs text-muted-foreground">
                {meta(o)}
              </span>
            </label>
          ))}
          {shown.length === 0 && (
            <p className="px-5 py-4 text-sm text-muted-foreground">
              No role matches “{query.trim()}”.
            </p>
          )}
          {hidden > 0 && shown.length > 0 && (
            <p className="px-5 py-2 text-xs text-muted-foreground">
              {pluralise(hidden, "more role")} hidden by the filter above.
            </p>
          )}
        </fieldset>
        <div className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-3.5">
          <span className="text-sm text-muted-foreground">
            {draft.size
              ? `${draft.size} of ${options.length} picked`
              : "No filter: every role"}
          </span>
          {draft.size > 0 && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setDraft(new Set())}
            >
              Clear
            </Button>
          )}
          <div className="ml-auto flex gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button"
              // In the project's role order, so the fetches key stably.
              onClick={() =>
                onApply(options.map((o) => o.name).filter((n) => draft.has(n)))
              }
            >
              {applyLabel(draft.size)}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ChipToggle({
  pressed,
  onClick,
  children,
}: {
  pressed: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className={cn(
        "h-8 rounded-full border px-3 text-xs font-medium transition-colors",
        pressed
          ? "border-primary bg-primary/10 text-foreground"
          : "border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {children}
    </button>
  );
}

/** The graph's line per role: whether it runs the agent, and its tools. */
function graphMeta(o: RoleOption): string {
  if (o.runs === false) return "can't run it";
  return `${o.runs ? "runs it · " : ""}${pluralise(o.tools, "tool")}`;
}
