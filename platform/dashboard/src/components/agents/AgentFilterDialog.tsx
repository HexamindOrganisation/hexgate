/**
 * The /agents Filters dialog: a draft of every filter, with how many agents
 * each option keeps, applied in one go. Roles open the role picker.
 */

import { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { RolePickerDialog } from "@/components/policy_files/RolePicker";
import {
  type Activity,
  type AgentFacts,
  type AgentFilters,
  type Delegation,
  NO_FILTERS,
  countWhere,
  matches,
} from "@/lib/agent-filters";
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACTIVITY: [Activity, string][] = [
  ["deny", "Has denials"],
  ["approval", "Waiting on approval"],
  ["quiet", "No calls"],
];
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
  applied,
  onApply,
  onClose,
}: {
  facts: AgentFacts[];
  /** Every role in the project. */
  roles: string[];
  applied: AgentFilters;
  onApply: (f: AgentFilters) => void;
  onClose: () => void;
}) {
  const [f, setF] = useState(applied);
  const [pickingRoles, setPickingRoles] = useState(false);
  const frameworks = useMemo(
    () =>
      [
        ...new Set(facts.map((a) => a.framework).filter(Boolean)),
      ].sort() as string[],
    [facts],
  );
  const kept = facts.filter((a) => matches(f, a)).length;
  const roleOptions = useMemo(
    () =>
      roles.map((name) => ({
        name,
        runs: null,
        tools: countWhere(facts, { roles: [name] }),
      })),
    [roles, facts],
  );

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
                  [
                    "registered",
                    "Registered",
                    countWhere(facts, { status: "registered" }),
                  ],
                  [
                    "unregistered",
                    "Not registered",
                    countWhere(facts, { status: "unregistered" }),
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
          <Section title="Last 24 hours">
            {ACTIVITY.map(([v, label]) => (
              <Check
                key={v}
                label={label}
                count={countWhere(facts, { activity: [v] })}
                checked={f.activity.includes(v)}
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
                  count={countWhere(facts, { frameworks: [fw] })}
                  checked={f.frameworks.includes(fw)}
                  onChange={() =>
                    setF((p) => ({ ...p, frameworks: flip(p.frameworks, fw) }))
                  }
                />
              ))}
            </Section>
          )}
          {roles.length > 0 && (
            <Section title="Role">
              <button
                type="button"
                onClick={() => setPickingRoles(true)}
                className="flex h-10 items-center gap-2 rounded-lg border border-input px-3 text-left text-[13px] font-medium hover:bg-accent/60"
              >
                {f.roles.length
                  ? `Agents ${f.roles.join(", ")} can run`
                  : "Agents a role can run · pick roles…"}
                <ChevronRight className="ml-auto size-3.5 text-muted-foreground" />
              </button>
            </Section>
          )}
          <Section title="Delegation">
            {DELEGATION.map(([v, label]) => (
              <Check
                key={v}
                label={label}
                count={countWhere(facts, { delegation: [v] })}
                checked={f.delegation.includes(v)}
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
        {pickingRoles && (
          <RolePickerDialog
            options={roleOptions}
            picked={f.roles}
            title="Agents these roles can run"
            description="Keeps the agents any picked role may run. An agent no role gates stays in."
            meta={(o) => `runs ${pluralise(o.tools, "agent")}`}
            applyLabel={(n) => (n ? `Use ${pluralise(n, "role")}` : "Any role")}
            onClose={() => setPickingRoles(false)}
            onApply={(next) => {
              setF((p) => ({ ...p, roles: next }));
              setPickingRoles(false);
            }}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="m-0 flex flex-col gap-0.5 border-0 p-0">
      <legend className="pb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
        {title}
      </legend>
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
}: {
  label: string;
  count: number;
  checked: boolean;
  onChange: () => void;
  mono?: boolean;
}) {
  return (
    <label className="flex min-h-10 cursor-pointer items-center gap-3">
      <input
        type="checkbox"
        checked={checked}
        onChange={onChange}
        className="size-4 accent-[hsl(var(--primary))]"
      />
      <span className={cn("text-sm", mono && "font-mono text-[13px]")}>
        {label}
      </span>
      <span className="ml-auto text-xs text-muted-foreground">{count}</span>
    </label>
  );
}
