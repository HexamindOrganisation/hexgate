/**
 * The /agents Filters dialog: a draft of every filter, with how many agents
 * each option keeps, applied in one go. A section whose data hasn't loaded is
 * off, and says why, rather than keep or hide agents on a guess.
 */

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { SearchField } from "@/components/ui/search-field";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  type Activity,
  type AgentFacts,
  type AgentFilters,
  type Delegation,
  ACTIVITY_LABEL,
  NO_FILTERS,
  countWhere,
  matches,
} from "@/lib/agent-filters";
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACTIVITY = Object.entries(ACTIVITY_LABEL) as [Activity, string][];
const DELEGATION: [Delegation, string][] = [
  ["reaches", "Reaches other agents"],
  ["called", "Called by other agents"],
];

/** Toggle `v` in a list. */
const flip = <T,>(xs: T[], v: T) =>
  xs.includes(v) ? xs.filter((x) => x !== v) : [...xs, v];

export function AgentFilterDialog({
  facts,
  roles,
  activityKnown,
  graphKnown,
  applied,
  onApply,
  onClose,
}: {
  facts: AgentFacts[];
  /** Every role in the project. */
  roles: string[];
  /** The 24h summary loaded. */
  activityKnown: boolean;
  /** The policy graph loaded (delegation and roles come from it). */
  graphKnown: boolean;
  applied: AgentFilters;
  onApply: (f: AgentFilters) => void;
  onClose: () => void;
}) {
  const [f, setF] = useState(applied);
  const [roleQuery, setRoleQuery] = useState("");
  const frameworks = useMemo(
    () =>
      [
        ...new Set(facts.map((a) => a.framework).filter(Boolean)),
      ].sort() as string[],
    [facts],
  );
  const shownRoles = useMemo(() => {
    const q = roleQuery.trim().toLowerCase();
    return q ? roles.filter((r) => r.toLowerCase().includes(q)) : roles;
  }, [roles, roleQuery]);
  const kept = facts.filter((a) => matches(f, a)).length;
  // Each option's count, once per agent list: typing in the role search
  // mustn't recount every option.
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    const put = (key: string, patch: Partial<AgentFilters>) =>
      m.set(key, countWhere(facts, patch));
    put("s:registered", { status: "registered" });
    put("s:unregistered", { status: "unregistered" });
    for (const [v] of ACTIVITY) put(`a:${v}`, { activity: [v] });
    for (const fw of frameworks) put(`f:${fw}`, { frameworks: [fw] });
    for (const r of roles) put(`r:${r}`, { roles: [r] });
    for (const [v] of DELEGATION) put(`d:${v}`, { delegation: [v] });
    return m;
  }, [facts, frameworks, roles]);

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="flex max-h-[85vh] max-w-[560px] flex-col gap-0 overflow-hidden p-0">
        <div className="px-5 pb-2 pt-5">
          <DialogTitle>Filter agents</DialogTitle>
        </div>
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 pb-4 pt-2">
          <Section title="Status">
            <div className="flex flex-wrap gap-2">
              {(
                [
                  ["any", "Any", facts.length],
                  ["registered", "Registered", counts.get("s:registered") ?? 0],
                  [
                    "unregistered",
                    "Not registered",
                    counts.get("s:unregistered") ?? 0,
                  ],
                ] as const
              ).map(([v, label, n]) => (
                <button
                  key={v}
                  type="button"
                  aria-pressed={f.status === v}
                  onClick={() => setF((p) => ({ ...p, status: v }))}
                  className={cn(
                    "h-9 rounded-full border px-3 text-[13px] font-medium transition-colors",
                    f.status === v
                      ? "border-primary bg-primary/10 text-foreground"
                      : "border-border text-muted-foreground hover:text-foreground",
                  )}
                >
                  {label} · {n}
                </button>
              ))}
            </div>
          </Section>
          <Section
            title="Last 24 hours"
            off={!activityKnown && "The last 24 hours haven't loaded."}
          >
            {ACTIVITY.map(([v, label]) => (
              <Check
                key={v}
                label={label}
                count={counts.get(`a:${v}`) ?? 0}
                checked={f.activity.includes(v)}
                disabled={!activityKnown}
                onChange={() =>
                  setF((p) => ({ ...p, activity: flip(p.activity, v) }))
                }
              />
            ))}
          </Section>
          {frameworks.length > 0 && (
            <Section title="Framework">
              {frameworks.map((fw) => (
                <Check
                  key={fw}
                  label={fw}
                  mono
                  count={counts.get(`f:${fw}`) ?? 0}
                  checked={f.frameworks.includes(fw)}
                  onChange={() =>
                    setF((p) => ({ ...p, frameworks: flip(p.frameworks, fw) }))
                  }
                />
              ))}
            </Section>
          )}
          {(roles.length > 0 || !graphKnown) && (
            <Section
              title="Run by role"
              off={!graphKnown && "The policy graph hasn't loaded."}
            >
              {roles.length > 8 && (
                <SearchField
                  value={roleQuery}
                  onChange={setRoleQuery}
                  placeholder={`Search ${pluralise(roles.length, "role")}`}
                  label="Search roles"
                  className="mb-1 h-9"
                />
              )}
              <div className="max-h-56 overflow-y-auto">
                {shownRoles.map((r) => (
                  <Check
                    key={r}
                    label={r}
                    mono
                    // What applying it keeps, ungated agents included.
                    count={counts.get(`r:${r}`) ?? 0}
                    checked={f.roles.includes(r)}
                    disabled={!graphKnown}
                    onChange={() =>
                      setF((p) => ({ ...p, roles: flip(p.roles, r) }))
                    }
                  />
                ))}
              </div>
              <p className="pt-1 text-xs text-muted-foreground">
                An agent no role gates stays in whatever is picked.
              </p>
            </Section>
          )}
          <Section
            title="Delegation"
            off={!graphKnown && "The policy graph hasn't loaded."}
          >
            {DELEGATION.map(([v, label]) => (
              <Check
                key={v}
                label={label}
                count={counts.get(`d:${v}`) ?? 0}
                checked={f.delegation.includes(v)}
                disabled={!graphKnown}
                onChange={() =>
                  setF((p) => ({ ...p, delegation: flip(p.delegation, v) }))
                }
              />
            ))}
          </Section>
        </div>
        <div className="flex flex-wrap items-center gap-3 border-t border-border px-5 py-3.5">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setF(NO_FILTERS)}
          >
            Clear all
          </Button>
          <div className="ml-auto flex gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="button" onClick={() => onApply(f)}>
              Show {pluralise(kept, "agent")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Section({
  title,
  off,
  children,
}: {
  title: string;
  /** Why the section is off, or false. */
  off?: string | false;
  children: React.ReactNode;
}) {
  return (
    <fieldset
      disabled={!!off}
      className={cn(
        "m-0 flex flex-col gap-0.5 border-0 p-0",
        off && "opacity-60",
      )}
    >
      <legend className="pb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </legend>
      {off && <p className="pb-1 text-xs text-muted-foreground">{off}</p>}
      {children}
    </fieldset>
  );
}

function Check({
  label,
  count,
  checked,
  onChange,
  mono,
  disabled,
}: {
  label: string;
  count: number;
  checked: boolean;
  onChange: () => void;
  mono?: boolean;
  disabled?: boolean;
}) {
  return (
    <label
      className={cn(
        "flex min-h-10 items-center gap-3",
        disabled ? "cursor-not-allowed" : "cursor-pointer",
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        className="size-4 accent-[hsl(var(--primary))]"
      />
      <span className={cn("text-sm", mono && "font-mono text-[13px]")}>
        {label}
      </span>
      {/* No count while the data behind it is missing. */}
      {!disabled && (
        <span className="ml-auto text-xs text-muted-foreground">{count}</span>
      )}
    </label>
  );
}
