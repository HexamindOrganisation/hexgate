/**
 * Which role one agent's graph is drawn for: a button that opens a dialog
 * with a searchable list. One role at a time, as the graph API resolves one
 * (several roles at once is #483); no role is every role.
 */

import { useMemo, useState } from "react";
import { ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { SearchField } from "@/components/ui/search-field";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import type { RoleOption } from "@/lib/agent-graph-view";
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Opens the role picker; says which role the graph is drawn for. */
export function RolesButton({
  total,
  picked,
  onClick,
  className,
}: {
  total: number;
  picked: string | null;
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
      {picked ? (
        <>
          Role · <span className="font-mono">{picked}</span>
        </>
      ) : (
        `Roles · all ${total}`
      )}
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
}: {
  options: RoleOption[];
  picked: string | null;
  onClose: () => void;
  /** null: every role. */
  onApply: (role: string | null) => void;
}) {
  const [query, setQuery] = useState("");
  // Only meaningful when the policy gates running the agent by role.
  const gated = options.some((o) => o.runs !== null);
  const runners = options.filter((o) => o.runs).length;
  // Narrowed to runners only when some role does run it.
  const [onlyRunners, setOnlyRunners] = useState(gated && runners > 0);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options.filter(
      (o) =>
        (!onlyRunners || o.runs !== false) &&
        (!q || o.name.toLowerCase().includes(q)),
    );
  }, [options, onlyRunners, query]);
  const hidden = options.length - shown.length;

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="flex max-h-[85vh] max-w-[480px] flex-col gap-0 overflow-hidden p-0">
        <div className="space-y-1.5 px-5 pb-3 pt-5">
          <DialogTitle>Show the graph for a role</DialogTitle>
          <DialogDescription>
            What a caller with this role may do through the agent.
          </DialogDescription>
        </div>
        <div className="space-y-2.5 px-5 pb-3">
          <SearchField
            value={query}
            onChange={setQuery}
            placeholder={`Search ${pluralise(options.length, "role")}`}
            label="Search roles"
          />
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
        <ul className="min-h-0 flex-1 overflow-y-auto border-t border-border py-1">
          <li>
            <RoleRow
              label="Every role"
              meta=""
              active={picked === null}
              onClick={() => onApply(null)}
            />
          </li>
          {shown.map((o) => (
            <li key={o.name}>
              <RoleRow
                label={o.name}
                mono
                meta={
                  o.runs === null ? "" : o.runs ? "runs it" : "can't run it"
                }
                active={picked === o.name}
                onClick={() => onApply(o.name)}
              />
            </li>
          ))}
          {shown.length === 0 && query.trim() && (
            <li className="px-5 py-3 text-sm text-muted-foreground">
              No role matches “{query.trim()}”.
            </li>
          )}
          {hidden > 0 && (
            <li className="px-5 py-2 text-xs text-muted-foreground">
              {pluralise(hidden, "more role")} hidden by the filter above.
            </li>
          )}
        </ul>
      </DialogContent>
    </Dialog>
  );
}

function RoleRow({
  label,
  meta,
  active,
  onClick,
  mono,
}: {
  label: string;
  meta: string;
  active: boolean;
  onClick: () => void;
  mono?: boolean;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "flex min-h-11 w-full items-center gap-3 px-5 text-left hover:bg-accent/60",
        active && "bg-accent/50",
      )}
    >
      <span className={cn("text-sm", mono && "font-mono text-[13px]")}>
        {label}
      </span>
      <span className="ml-auto text-xs text-muted-foreground">{meta}</span>
    </button>
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
