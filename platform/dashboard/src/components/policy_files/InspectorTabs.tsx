import { useMemo, useState } from "react";
import { dump } from "js-yaml";
import {
  AlertTriangle,
  FlaskConical,
  Info,
  ListChecks,
  Network,
} from "lucide-react";

import type { PolicyFileDraft, PolicyLint, ResolvedPolicy } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { PolicyGraphDialog } from "./PolicyGraphDialog";
import { TestPanel } from "./TestPanel";
import { cn } from "@/lib/utils";

type Tab = "resolved" | "lints" | "test";

function modeBadge(mode: string | undefined) {
  if (mode === "allow") return "allow" as const;
  if (mode === "deny") return "deny" as const;
  if (mode === "approval_required") return "approval" as const;
  return "default" as const;
}

type ToolKind = "tool" | "mcp" | "reach" | "admission";
const _KIND_ORDER: Record<ToolKind, number> = {
  tool: 0,
  mcp: 1,
  reach: 2,
  admission: 3,
};

/** The resolved policy folds agent-level policy into the same tool map: reach
 * lowers to ``agent.tool:``/``agent.handoff:`` and admission to ``agent.run``.
 * Classify a raw key so those show as reach/admission with a readable label
 * instead of a raw ``agent.*`` tool row. */
function classifyTool(key: string): {
  kind: ToolKind;
  label: string;
  tag?: string;
} {
  if (key === "agent.run") {
    return { kind: "admission", label: "start this agent", tag: "admission" };
  }
  const reach = /^agent\.(tool|handoff):(.+)$/.exec(key);
  if (reach) {
    return {
      kind: "reach",
      label: reach[2],
      tag: reach[1] === "tool" ? "as tool" : "handoff",
    };
  }
  if (key.startsWith("mcp-")) {
    return { kind: "mcp", label: key.slice(4), tag: "mcp" };
  }
  // Any other lowered agent-level key (a future agent.* form) still reads as an
  // agent edge, never a raw `agent.foo` tool row.
  if (key.startsWith("agent.")) {
    return { kind: "reach", label: key.slice("agent.".length), tag: "agent" };
  }
  return { kind: "tool", label: key };
}

/**
 * Right pane: the composed policy (Resolved), analyzer lints (Lints), and the
 * decision tester (Test), plus a policy-graph launcher. Resolved + Lints
 * reflect the unsaved draft when one is active (the page feeds preview results
 * through); otherwise stored state.
 */
export function InspectorTabs({
  projectId,
  resolved,
  lints,
  draft,
  resolves,
  modular,
  previewing,
  inspectAgent,
  onInspectAgentChange,
}: {
  projectId: string;
  resolved: ResolvedPolicy | undefined | null;
  lints: PolicyLint[];
  draft: PolicyFileDraft | null;
  resolves: boolean;
  modular: boolean;
  previewing: boolean;
  inspectAgent: string;
  onInspectAgentChange: (agent: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("resolved");
  const [graphOpen, setGraphOpen] = useState(false);
  const errorCount = lints.filter((l) => l.severity === "error").length;
  // Roles come from whatever resolved — a compose project has no separate role
  // registry; the resolved policy's keys are the roles that composed.
  const roleNames = useMemo(
    () => (resolved ? Object.keys(resolved).sort() : []),
    [resolved],
  );

  return (
    <div className="h-full flex flex-col">
      <div className="flex items-center gap-1 px-2 py-2 border-b border-border">
        <TabButton
          active={tab === "resolved"}
          onClick={() => setTab("resolved")}
          Icon={ListChecks}
          label="Resolved"
        />
        <TabButton
          active={tab === "lints"}
          onClick={() => setTab("lints")}
          Icon={AlertTriangle}
          label="Lints"
          count={lints.length}
          danger={errorCount > 0}
        />
        <TabButton
          active={tab === "test"}
          onClick={() => setTab("test")}
          Icon={FlaskConical}
          label="Test"
        />
        {previewing && (
          <span className="text-[10px] text-muted-foreground animate-pulse">
            previewing…
          </span>
        )}
        {modular && (
          <button
            onClick={() => setGraphOpen(true)}
            title="Open the policy graph"
            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            <Network size={13} />
            Graph
          </button>
        )}
      </div>
      {graphOpen && (
        <PolicyGraphDialog
          open={graphOpen}
          onOpenChange={setGraphOpen}
          projectId={projectId}
          roleNames={roleNames}
        />
      )}
      <div className="flex-1 overflow-hidden">
        {tab === "resolved" && (
          <ResolvedTab
            resolved={resolved}
            resolves={resolves}
            modular={modular}
            roleNames={roleNames}
            inspectAgent={inspectAgent}
            onInspectAgentChange={onInspectAgentChange}
          />
        )}
        {tab === "lints" && <LintsTab lints={lints} />}
        {tab === "test" && (
          <TestPanel
            projectId={projectId}
            roleNames={roleNames}
            draft={draft}
            resolves={resolves}
          />
        )}
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  Icon,
  label,
  count,
  danger,
}: {
  active: boolean;
  onClick: () => void;
  Icon: typeof Info;
  label: string;
  count?: number;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-medium transition-colors",
        active
          ? "bg-primary text-primary-foreground"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      <Icon className="size-3" />
      {label}
      {count !== undefined && count > 0 && (
        <span
          className={cn(
            "rounded-full px-1.5 text-[10px]",
            danger ? "bg-deny/20 text-deny" : "bg-muted text-muted-foreground",
          )}
        >
          {count}
        </span>
      )}
    </button>
  );
}

function ResolvedTab({
  resolved,
  resolves,
  modular,
  roleNames,
  inspectAgent,
  onInspectAgentChange,
}: {
  resolved: ResolvedPolicy | undefined | null;
  resolves: boolean;
  modular: boolean;
  roleNames: string[];
  inspectAgent: string;
  onInspectAgentChange: (agent: string) => void;
}) {
  const [role, setRole] = useState<string>("");
  // Local buffer so typing the agent name doesn't fire a resolve+preview
  // round-trip per keystroke — commit on blur / Enter instead. Re-sync to the
  // prop when it changes externally (e.g. a project switch resets it) via the
  // render-phase adjust-on-prop-change pattern.
  const [agentInput, setAgentInput] = useState(inspectAgent);
  const [syncedAgent, setSyncedAgent] = useState(inspectAgent);
  if (inspectAgent !== syncedAgent) {
    setSyncedAgent(inspectAgent);
    setAgentInput(inspectAgent);
  }
  const commitAgent = () => {
    const v = agentInput.trim() || "*";
    if (v !== inspectAgent) onInspectAgentChange(v);
  };
  const [view, setView] = useState<"table" | "yaml">("table");
  const active = roleNames.includes(role) ? role : (roleNames[0] ?? "");

  if (!modular || !resolves || !resolved || roleNames.length === 0) {
    return (
      <div className="h-full grid place-items-center px-6 text-center">
        <p className="text-xs text-muted-foreground">
          {!resolves
            ? "The policy doesn't compose. See the Lints tab."
            : "Add a policy.yaml with grants to see the composed policy. (Classic projects enforce each agent's own policy.)"}
        </p>
      </div>
    );
  }

  const policy = resolved[active];
  const tools = policy?.tools ?? {};
  // Classify each key once (not per comparison + again per render), then group
  // plain tools, then MCP, reach, and admission — the agent.* keys read last.
  const rows = Object.keys(tools)
    .map((name) => ({ name, ...classifyTool(name), tp: tools[name] }))
    .sort(
      (a, b) =>
        _KIND_ORDER[a.kind] - _KIND_ORDER[b.kind] ||
        a.label.localeCompare(b.label),
    );
  const defaultMode = policy?.default_policy?.mode;

  return (
    <div className="h-full flex flex-col">
      {/* Which executing agent's column to inspect. "*" is the generic view; a
          named agent shows its own composed policy (a sub-agent it may reach, a
          capability it's denied). Committed on blur / Enter, so the resolve +
          preview fire once per agent, not per keystroke. */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border/60 text-[11px]">
        <span className="text-muted-foreground">Agent</span>
        <input
          value={agentInput}
          onChange={(e) => setAgentInput(e.target.value)}
          onBlur={commitAgent}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              commitAgent();
              e.currentTarget.blur();
            }
          }}
          placeholder="*"
          className="w-32 rounded border border-border bg-background px-1.5 py-0.5 font-mono text-[11px]"
        />
      </div>
      <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border">
        <div className="flex flex-wrap items-center gap-1 min-w-0">
          {roleNames.map((r) => (
            <button
              key={r}
              onClick={() => setRole(r)}
              className={cn(
                "rounded px-2 py-0.5 text-xs font-mono",
                r === active
                  ? "bg-primary/15 text-primary font-medium"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {r}
            </button>
          ))}
        </div>
        <div className="flex items-center rounded-md border border-border p-0.5 shrink-0">
          {(["table", "yaml"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={cn(
                "rounded px-2 py-0.5 text-[11px] font-medium capitalize transition-colors",
                view === v
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {v}
            </button>
          ))}
        </div>
      </div>
      {view === "yaml" ? (
        <div className="flex-1 overflow-auto scrollbar-thin p-3">
          {/* The composed policy for this role as one document — boundaries +
              the role's capabilities merged. Read-only; edits happen in the
              files. */}
          <pre className="text-[11px] font-mono leading-relaxed whitespace-pre">
            {dumpPolicy(policy)}
          </pre>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto scrollbar-thin p-3 space-y-1">
          {defaultMode && (
            <div className="flex items-center justify-between gap-2 py-1 text-xs border-b border-border/50">
              <span className="text-muted-foreground italic">default</span>
              <Badge variant={modeBadge(defaultMode)}>{defaultMode}</Badge>
            </div>
          )}
          {rows.length === 0 ? (
            <p className="text-xs text-muted-foreground pt-2">
              No tools granted for this role.
            </p>
          ) : (
            rows.map(({ name, kind, label, tag, tp }) => {
              const constraints = tp?.constraints ?? [];
              return (
                <div
                  key={name}
                  className="flex items-start justify-between gap-2 py-1 text-xs border-b border-border/50"
                >
                  <div className="min-w-0">
                    <span className="font-mono">{label}</span>
                    {tag && (
                      <span
                        className={cn(
                          "ml-1.5 rounded px-1 py-0.5 text-[9px] uppercase tracking-wider",
                          kind === "admission"
                            ? "bg-primary/15 text-primary"
                            : "bg-muted text-muted-foreground",
                        )}
                      >
                        {tag}
                      </span>
                    )}
                    {constraints.length > 0 && (
                      <ul className="mt-0.5 text-[11px] text-muted-foreground font-mono space-y-0.5">
                        {constraints.map((c, i) => (
                          <li key={i}>{c}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                  <Badge variant={modeBadge(tp?.mode)}>{tp?.mode ?? "—"}</Badge>
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}

/** The resolved policy for one role, as a single YAML document. Drops the
 * catch-all `[key: string]` passthrough so only the real policy fields show. */
function dumpPolicy(policy: ResolvedPolicy[string] | undefined): string {
  if (!policy) return "# no policy for this role\n";
  const { default_policy, tools } = policy;
  const doc: Record<string, unknown> = {};
  if (default_policy) doc.default_policy = default_policy;
  doc.tools = tools ?? {};
  return dump(doc, { sortKeys: false, lineWidth: 100 });
}

function LintsTab({ lints }: { lints: PolicyLint[] }) {
  if (lints.length === 0) {
    return (
      <div className="h-full grid place-items-center px-6 text-center">
        <p className="text-xs text-muted-foreground">
          No lints — the policy composes cleanly.
        </p>
      </div>
    );
  }
  return (
    <div className="h-full overflow-y-auto scrollbar-thin p-3 space-y-1.5">
      {lints.map((l, i) => (
        <div
          key={i}
          className={cn(
            "rounded border p-2 text-xs",
            l.severity === "error"
              ? "border-deny/30 bg-deny/5"
              : l.severity === "warning"
                ? "border-approval/30 bg-approval/5"
                : "border-border bg-muted/30",
          )}
        >
          <div className="flex items-center gap-1.5">
            <span
              className={cn(
                "font-mono text-[10px] uppercase",
                l.severity === "error"
                  ? "text-deny"
                  : l.severity === "warning"
                    ? "text-approval"
                    : "text-muted-foreground",
              )}
            >
              {l.severity}
            </span>
            <span className="font-mono text-[10px] text-muted-foreground">
              {l.code}
            </span>
          </div>
          <p className="mt-0.5 text-foreground/90">{l.message}</p>
          {(l.source || l.role || l.tool) && (
            <p className="mt-0.5 text-[10px] text-muted-foreground font-mono">
              {[
                l.role && `role:${l.role}`,
                l.source,
                l.tool && `tool:${l.tool}`,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
