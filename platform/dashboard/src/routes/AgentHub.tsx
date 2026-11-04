import { useMemo, useState } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useNavigate,
  useOutletContext,
  useParams,
} from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Ban,
  ChevronRight,
  Network,
  ScrollText,
  ShieldCheck,
} from "lucide-react";

import {
  ApiError,
  api,
  type AgentManifestView,
  type AuditDecisionRow,
  type PolicyGraph,
  type PolicyGraphEdge,
  type PolicyMatrix,
  type SubagentRef,
} from "@/lib/api";
import {
  usePolicyFiles,
  usePolicyGraph,
  usePolicyMatrix,
} from "@/lib/policy_files";
import { ENTRY_FILE } from "@/lib/file_tree";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { useAuditSummary24h } from "@/lib/audit-summary";
import { auditUrl } from "@/lib/audit-filters";
import { agentReachEdges, agentSubgraph } from "@/lib/agent-graph";
import { formatDate, pluralise } from "@/lib/format";
import {
  PolicyFlow,
  VerdictTag,
} from "@/components/policy_files/PolicyGraphDialog";
import { ResolvedGrid } from "@/components/policy_files/InspectorTabs";
import { EventsTable } from "@/components/audit/pieces";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { cn } from "@/lib/utils";
import { DetailDrawer } from "./Audit";
import { ManifestView, type RoleMode } from "./Agents";
import { PlaygroundLive } from "./Playground";
import { usePlaygroundServedAgent } from "@/lib/playground";

type Activity = { all: number; deny: number; needs_approval: number };

/** Tools + skills + handoff sub-agents. An agent-as-tool sub-agent is already
 * one of its tools, so only handoffs add to the count. */
function resourceCount(agent: AgentManifestView) {
  const m = agent.manifest;
  const tools = new Set(m?.tools?.map((t) => t.name));
  const subs = new Set(
    m?.subagents?.map((s) => s.name).filter((n) => !tools.has(n)),
  );
  return tools.size + (m?.skills?.length ?? 0) + subs.size;
}

/** A sub-agent from the manifest, with its policy edge when the project has a
 * graph, and its own 24h call count (null until the summary lands). */
type SubAgentRow = SubagentRef & {
  edge: PolicyGraphEdge | null;
  /** No edge names it: denied once the policy declares any reach (closed
   * world, R-AGENT-002), else never checked. Null without a graph. */
  unnamed: "denied" | "ungated" | null;
  /** Registered in this project, so its hub exists. */
  registered: boolean;
  calls: number | null;
};

/**
 * `/agents/:name` — the single-agent hub. This is the first step of the
 * agent-first navigation: everything about one agent lives on its own page
 * instead of being scattered across Agents / Policies / Audit. For now the
 * body is the agent's manifest + per-role policy (reusing `ManifestView` and
 * the engine matrix); later PRs turn the header into tabs (Overview graph,
 * Resources, Playground, Decision log).
 */
export function AgentHub() {
  const { name } = useParams<{ name: string }>();
  const scope = useProjectScoped();

  const manifests = useQuery({
    queryKey: ["agent-manifests", scope.projectId],
    queryFn: () => api.listAgentManifests(scope.projectId as string),
    enabled: !!scope.projectId,
  });
  const agent = manifests.data?.find((m) => m.name === name) ?? null;

  // 24h activity — shares Home's cache entry.
  const summary = useAuditSummary24h(scope.projectId, { poll: true });
  const summaryData = summary.data;
  const byAgent = useMemo(
    () => new Map(summaryData?.by_agent.map((r) => [r.key, r])),
    [summaryData],
  );
  const activity: Activity | null = useMemo(
    () =>
      agent && summaryData
        ? (byAgent.get(agent.name) ?? { all: 0, deny: 0, needs_approval: 0 })
        : null,
    [agent, summaryData, byAgent],
  );

  // Roles x tools for this agent, as the engine tabulates them. Only projects
  // with a policy.yaml compose; others have no matrix.
  const policyFiles = usePolicyFiles(scope.projectId);
  const policyFilesLoading = policyFiles.isLoading;
  const modular = !!policyFiles.data?.some((f) => f.name === ENTRY_FILE);
  const policyFileNames = useMemo(
    () => policyFiles.data?.map((f) => f.name) ?? [],
    [policyFiles.data],
  );
  const toolNames = useMemo(
    () => agent?.manifest?.tools?.map((t) => t.name) ?? [],
    [agent],
  );
  const matrixQuery = usePolicyMatrix(
    scope.projectId,
    agent?.name ?? "",
    modular && !!agent,
    toolNames,
  );
  // Forward null (not undefined) on error so ResolvedGrid shows its error
  // branch instead of spinning on "Loading grid…" forever.
  const matrix = matrixQuery.isError ? null : matrixQuery.data;
  const roleModes = useMemo(() => {
    if (!matrix) return undefined;
    return (tool: string): RoleMode[] =>
      matrix.roles.map((role) => {
        const cell = matrix.cells[tool]?.[role];
        return {
          role,
          mode: cell?.mode,
          constraint: cell?.constraint ?? null,
          defaulted: !matrix.tools.includes(tool),
        };
      });
  }, [matrix]);

  // The agent's local graph (its roles, tools and reached agents), filtered
  // from the project graph — no backend change. Classic projects 404 (no
  // graph), handled by the placeholder below.
  const graphQuery = usePolicyGraph(scope.projectId, undefined, !!agent);
  const graph = graphQuery.data;
  // Classic (non-compose) projects 404 the graph endpoint — that's "no graph",
  // not a failure; anything else is a real error.
  const graph404 =
    graphQuery.error instanceof ApiError && graphQuery.error.status === 404;
  const subgraph = useMemo(
    () => (graph && agent ? agentSubgraph(graph, agent.name) : null),
    [graph, agent],
  );
  // Sub-agents come from the manifest (every project has one); the graph only
  // adds each reach's verdict, where the project composes a policy.
  const subAgents = useMemo<SubAgentRow[] | null>(() => {
    const refs = agent?.manifest?.subagents;
    if (!refs) return null;
    // agentSubgraph is empty (not null) when the agent isn't in the graph.
    const edges =
      subgraph && subgraph.nodes.length > 0
        ? agentReachEdges(subgraph, agent.name)
        : null;
    const names = new Set(manifests.data?.map((m) => m.name));
    // The manifest may list a (name, via) twice (e.g. two as_tool mounts).
    const unique = [
      ...new Map(refs.map((r) => [`${r.name}:${r.via}`, r])).values(),
    ];
    return unique.map((r) => {
      const edge = edges?.get(`${r.name}:${r.via}`) ?? null;
      return {
        ...r,
        edge,
        unnamed: !edges || edge ? null : edges.size > 0 ? "denied" : "ungated",
        registered: names.has(r.name),
        calls: summaryData ? (byAgent.get(r.name)?.all ?? 0) : null,
      };
    });
  }, [agent, manifests.data, subgraph, summaryData, byAgent]);

  const canManageBans = useCanManageBans();

  // The playground relay is project-scoped (one `hexgate serve` at a time), so
  // this agent's Playground tab is only live when it is the agent being served.
  // Gate on the agent name (stable across socket blips), not the transient
  // `online` flag — else a reconnect would flap the tab and drop the chat.
  const served = usePlaygroundServedAgent({ projectId: scope.projectId ?? "" });
  const playgroundLive = !!agent && served.agentName === agent.name;

  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="agents" />;
  }

  if (manifests.isPending) {
    return (
      <div className="mx-auto max-w-[960px] p-8 text-sm text-muted-foreground">
        Loading agent…
      </div>
    );
  }

  // A failed fetch is not a missing agent — keep the two distinct.
  if (manifests.isError && !manifests.data) {
    return (
      <div className="mx-auto max-w-[960px] space-y-4">
        <Breadcrumb name={name ?? ""} />
        <Card className="grid place-items-center p-12 text-center text-sm text-muted-foreground">
          Couldn't load this agent. Try refreshing the page.
        </Card>
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="mx-auto max-w-[960px] space-y-4">
        <Breadcrumb name={name ?? ""} />
        <Card className="grid place-items-center gap-2 p-12 text-center text-sm text-muted-foreground">
          <span>
            No agent named{" "}
            <span className="font-mono text-foreground">{name}</span> in this
            project.
          </span>
          <Link to="/agents" className="text-primary hover:underline">
            Back to agents
          </Link>
        </Card>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[960px] space-y-4">
      <Breadcrumb name={agent.name} />
      <AgentHeader agent={agent} canManageBans={canManageBans} />
      <HubTabs
        resources={resourceCount(agent)}
        playgroundLive={playgroundLive}
      />
      <Outlet
        context={
          {
            agent,
            roleModes,
            matrix,
            modular,
            policyFilesLoading,
            policyFileNames,
            activity,
            activityError: summary.isError && !summary.data,
            subgraph,
            subAgents,
            graphLoading: graphQuery.isLoading,
            graphError: graphQuery.isError && !graph404,
            playgroundLive,
            servedAgent: served.agentName,
            serveOnline: served.online,
          } satisfies HubContext
        }
      />
    </div>
  );
}

/** Shared state the hub computes once and its tabs read via the Outlet. */
export interface HubContext {
  agent: AgentManifestView;
  roleModes?: (tool: string) => RoleMode[];
  /** Engine-tabulated roles x tools for this agent; undefined while loading,
   * null on error, absent on a classic (non-modular) project. */
  matrix: PolicyMatrix | undefined | null;
  modular: boolean;
  /** Policy files still loading — `modular` is only meaningful once false. */
  policyFilesLoading: boolean;
  /** Names of the policy files that govern this project. */
  policyFileNames: string[];
  activity: Activity | null;
  activityError: boolean;
  subgraph: PolicyGraph | null;
  /** The manifest's sub-agents; null when it records none. */
  subAgents: SubAgentRow[] | null;
  graphLoading: boolean;
  graphError: boolean;
  /** This agent is the one currently served, so its Playground tab is live. */
  playgroundLive: boolean;
  /** The agent the serve relay currently exposes (any), or null if none. */
  servedAgent: string | null;
  /** A serve is attached, even if it hasn't announced its agent yet. */
  serveOnline: boolean;
}

/** The Overview tab — the agent's graph + KPI strip. */
export function AgentOverviewTab() {
  const ctx = useOutletContext<HubContext>();
  return (
    <AgentOverview
      agent={ctx.agent}
      activity={ctx.activity}
      activityError={ctx.activityError}
      subgraph={ctx.subgraph}
      graphLoading={ctx.graphLoading}
      graphError={ctx.graphError}
    />
  );
}

/** The Resources tab — the manifest's tools, guards, skills and the
 * sub-agents it reaches. */
export function AgentResourcesTab() {
  const ctx = useOutletContext<HubContext>();
  return (
    <ManifestView
      agent={ctx.agent}
      roleModes={ctx.roleModes}
      reaches={
        <ReachesSection
          agent={ctx.agent.name}
          subAgents={ctx.subAgents}
          unregistered={ctx.agent.manifest === null}
          // agentSubgraph is empty (not null) when the agent isn't in the graph.
          inGraph={!!ctx.subgraph && ctx.subgraph.nodes.length > 0}
          callsError={ctx.activityError}
        />
      }
    />
  );
}

const VIA_LABEL: Record<SubagentRef["via"], string> = {
  tool: "agent-as-tool",
  handoff: "handoff",
};

/** Sub-agents count as resources: what they can call is reachable from here.
 * One row per (sub-agent, via), as the manifest records them. */
function ReachesSection({
  agent,
  subAgents,
  unregistered,
  inGraph,
  callsError,
}: {
  agent: string;
  subAgents: SubAgentRow[] | null;
  unregistered: boolean;
  /** The project's policy graph has this agent, so rows carry verdicts. */
  inGraph: boolean;
  callsError: boolean;
}) {
  return (
    <section className="rounded-xl border border-border bg-card">
      <div className="px-5 py-4 border-b border-border flex items-center gap-2">
        <Network className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Reaches</span>
        {subAgents !== null && (
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {subAgents.length}
          </Badge>
        )}
        {inGraph && (
          <Link
            to={`/agents/${encodeURIComponent(agent)}`}
            className="ml-auto text-xs text-muted-foreground hover:text-foreground"
          >
            Open graph →
          </Link>
        )}
      </div>
      {subAgents === null || subAgents.length === 0 ? (
        <p className="px-5 py-4 text-xs text-muted-foreground">
          {unregistered
            ? "Agent not registered yet — run `hexgate register` to populate."
            : "No sub-agents recorded on its manifest."}
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {subAgents.map((s) => (
            <SubAgentItem
              key={`${s.name}:${s.via}`}
              sub={s}
              callsError={callsError}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function SubAgentItem({
  sub,
  callsError,
}: {
  sub: SubAgentRow;
  callsError: boolean;
}) {
  return (
    <li className="px-5 py-4 flex flex-wrap items-center gap-3">
      {sub.registered ? (
        <Link
          to={`/agents/${encodeURIComponent(sub.name)}`}
          className="font-mono text-sm text-foreground hover:underline"
        >
          {sub.name}
        </Link>
      ) : (
        <span
          className="font-mono text-sm text-foreground"
          title="Not registered in this project yet"
        >
          {sub.name}
        </span>
      )}
      <Badge variant="outline" className="font-mono text-[10px] py-0">
        {VIA_LABEL[sub.via]}
      </Badge>
      <span className="text-xs text-muted-foreground">
        {callsError
          ? "calls unavailable"
          : `${sub.calls === null ? "—" : sub.calls.toLocaleString()} calls · 24h (all callers)`}
      </span>
      {sub.unnamed === "denied" && (
        <span
          className="ml-auto"
          title="No reach rule names it, and reach is closed-world once a policy declares any — so it's denied."
        >
          <VerdictTag verdict="deny" />
        </span>
      )}
      {sub.unnamed === "ungated" && (
        <span
          className="ml-auto text-[11px] text-muted-foreground"
          title="Its policy declares no reach, so this isn't checked."
        >
          not gated
        </span>
      )}
      {sub.edge && (
        <span className="ml-auto flex items-center gap-2">
          {sub.edge.roles.length > 0 && (
            <span className="font-mono text-[11px] text-muted-foreground">
              {sub.edge.roles.join(", ")}
            </span>
          )}
          <VerdictTag
            verdict={sub.edge.verdict}
            constraints={sub.edge.constraints}
          />
        </span>
      )}
    </li>
  );
}

/** The Policies tab — the engine-resolved roles x tools grid for this agent. */
export function AgentPoliciesTab() {
  const ctx = useOutletContext<HubContext>();
  return (
    <AgentPolicies
      agent={ctx.agent}
      matrix={ctx.matrix}
      modular={ctx.modular}
      filesLoading={ctx.policyFilesLoading}
      fileNames={ctx.policyFileNames}
    />
  );
}

/** The Decision log tab — the audit log pre-scoped to this agent. */
export function AgentDecisionsTab() {
  const ctx = useOutletContext<HubContext>();
  return <AgentDecisions agent={ctx.agent} />;
}

/** The Playground tab — a live chat with THIS agent, embedded in the hub. The
 * relay is project-scoped (one `hexgate serve` at a time), so the tab only
 * shows a session when this agent is the one being served; otherwise it's a
 * muted "not served" state (the nav tab is unclickable, but a direct URL or a
 * mid-session serve-switch can still land here). */
export function AgentPlaygroundTab() {
  const ctx = useOutletContext<HubContext>();
  const { projectId } = useProjectScoped();
  if (!projectId) return null;
  if (!ctx.playgroundLive) {
    return (
      <PlaygroundNotServed
        agent={ctx.agent.name}
        servedAgent={ctx.servedAgent}
        serveOnline={ctx.serveOnline}
      />
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-meta text-muted-foreground">
        Live session with{" "}
        <span className="font-mono text-foreground">{ctx.agent.name}</span>.
        Every tool call is checked against its policy and recorded in Audit.
      </p>
      <PlaygroundLive projectId={projectId} embedded />
    </div>
  );
}

/** Shown on the Playground tab when this agent isn't the one being served. */
function PlaygroundNotServed({
  agent,
  servedAgent,
  serveOnline,
}: {
  agent: string;
  servedAgent: string | null;
  serveOnline: boolean;
}) {
  const otherAgent = servedAgent && servedAgent !== agent ? servedAgent : null;
  return (
    <Card className="grid place-items-center gap-2 p-12 text-center text-sm text-muted-foreground">
      <span>
        <span className="font-mono text-foreground">{agent}</span> isn't being
        served.
      </span>
      <span>
        Run <span className="font-mono text-foreground">hexgate serve</span> for
        this agent to test it here.
      </span>
      {otherAgent ? (
        <span className="text-meta">
          The playground is currently serving{" "}
          <span className="font-mono text-foreground">{otherAgent}</span>.
        </span>
      ) : serveOnline ? (
        <span className="text-meta">A serve session is starting…</span>
      ) : null}
    </Card>
  );
}

function HubTabs({
  resources,
  playgroundLive,
}: {
  resources: number;
  playgroundLive: boolean;
}) {
  const tab =
    "border-b-2 px-3 py-2 text-sm transition-colors -mb-px flex items-center gap-1.5";
  const cls = ({ isActive }: { isActive: boolean }) =>
    cn(
      tab,
      isActive
        ? "border-action font-medium text-foreground"
        : "border-transparent text-muted-foreground hover:text-foreground",
    );
  return (
    <nav
      aria-label="Agent sections"
      className="flex gap-1 border-b border-border"
    >
      <NavLink to="." end className={cls}>
        Overview
      </NavLink>
      <NavLink to="resources" className={cls}>
        Resources
        <span className="rounded bg-muted px-1.5 text-[11px] text-muted-foreground">
          {resources}
        </span>
      </NavLink>
      <NavLink to="policies" className={cls}>
        Policies
      </NavLink>
      {/* The playground is only live when THIS agent is the one being served;
          otherwise the tab is muted and unclickable (no link to navigate). */}
      {playgroundLive ? (
        <NavLink to="playground" className={cls}>
          Playground
        </NavLink>
      ) : (
        <span
          className={cn(
            tab,
            "cursor-not-allowed border-transparent opacity-50",
          )}
          aria-disabled="true"
          title="Run `hexgate serve` for this agent to test it in the playground"
        >
          Playground
        </span>
      )}
      <NavLink to="decisions" className={cls}>
        Decision log
      </NavLink>
    </nav>
  );
}

function Breadcrumb({ name }: { name: string }) {
  return (
    <nav
      aria-label="Breadcrumb"
      className="flex items-center gap-1.5 text-meta text-muted-foreground"
    >
      <Link to="/agents" className="hover:text-foreground">
        Agents
      </Link>
      <ChevronRight className="size-3.5" />
      <span className="font-mono text-foreground">{name}</span>
    </nav>
  );
}

/** Overview: the agent's graph as the centrepiece, with a KPI strip. */
function AgentOverview({
  agent,
  activity,
  activityError,
  subgraph,
  graphLoading,
  graphError,
}: {
  agent: AgentManifestView;
  activity: Activity | null;
  /** The 24h summary failed (vs still loading) — surface it, don't fake 0s. */
  activityError: boolean;
  subgraph: PolicyGraph | null;
  graphLoading: boolean;
  graphError: boolean;
}) {
  const missing = activity === null;
  const num = (n: number | undefined) =>
    missing ? "—" : (n ?? 0).toLocaleString();
  const kpis: { label: string; value: string; className?: string }[] = [
    { label: "Calls · 24h", value: num(activity?.all) },
    { label: "Denied", value: num(activity?.deny), className: "text-deny" },
    {
      label: "Approvals",
      value: num(activity?.needs_approval),
      className: "text-approval",
    },
    { label: "Resources", value: `${resourceCount(agent)}` },
  ];

  // Caption counts come from the agent's OWN outbound edges in the graph, so
  // "can reach" never includes a caller that reaches into it, and MCP servers
  // aren't mislabelled as tools.
  const kindById = new Map(subgraph?.nodes.map((n) => [n.id, n.kind]) ?? []);
  const selfId = subgraph?.nodes.find(
    (n) => n.kind === "agent" && n.label === agent.name,
  )?.id;
  const out = selfId
    ? (subgraph?.edges.filter((e) => e.source === selfId) ?? [])
    : [];
  const reached = (kind: string) =>
    new Set(
      out.filter((e) => kindById.get(e.target) === kind).map((e) => e.target),
    ).size;
  const caption = [
    reached("tool") && pluralise(reached("tool"), "tool"),
    reached("mcp") && pluralise(reached("mcp"), "server"),
    reached("agent") && pluralise(reached("agent"), "agent"),
  ]
    .filter(Boolean)
    .join(", ");

  const hasGraph = !graphLoading && !!subgraph && subgraph.nodes.length > 1;
  return (
    <Card className="overflow-hidden p-0">
      <div className="grid grid-cols-2 gap-px border-b border-border bg-border sm:grid-cols-4">
        {kpis.map((k) => (
          <div key={k.label} className="bg-card px-4 py-3">
            <div className="text-meta text-muted-foreground">{k.label}</div>
            <div
              className={cn(
                "mt-0.5 text-[20px] font-semibold tabular-nums",
                k.className,
              )}
            >
              {k.value}
            </div>
          </div>
        ))}
      </div>
      {activityError && (
        <div className="border-b border-border px-4 py-2 text-meta text-deny">
          Couldn't load the last 24 hours of activity.
        </div>
      )}
      <div className="px-4 py-2.5 text-meta text-muted-foreground">
        What <span className="font-mono text-foreground">{agent.name}</span> can
        reach{hasGraph && caption && ` — ${caption}`}
      </div>
      {graphLoading ? (
        <Placeholder>Loading graph…</Placeholder>
      ) : hasGraph ? (
        <div className="h-[420px] border-t border-border">
          <PolicyFlow graph={subgraph} />
        </div>
      ) : graphError ? (
        <Placeholder>Couldn't load the graph. Refresh to retry.</Placeholder>
      ) : agent.manifest === null ? (
        <Placeholder>Register the agent to see its graph.</Placeholder>
      ) : subgraph && subgraph.nodes.length <= 1 ? (
        <Placeholder>
          No roles, tools or reach mapped to this agent yet.
        </Placeholder>
      ) : (
        <Placeholder>
          No policy graph — add a policy.yaml to map roles, tools and reach.
        </Placeholder>
      )}
    </Card>
  );
}

function Placeholder({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid h-40 place-items-center border-t border-border px-6 text-center text-meta text-muted-foreground">
      {children}
    </div>
  );
}

/** Policies: the engine-resolved roles x tools grid for this agent, plus the
 * files that govern it. Read-only here — editing stays in the Policies page. */
function AgentPolicies({
  agent,
  matrix,
  modular,
  filesLoading,
  fileNames,
}: {
  agent: AgentManifestView;
  matrix: PolicyMatrix | undefined | null;
  modular: boolean;
  filesLoading: boolean;
  fileNames: string[];
}) {
  const navigate = useNavigate();
  const policyHref = `/policies?agent=${encodeURIComponent(agent.name)}`;

  // `modular` is only meaningful once the files resolve — guard the window
  // where it's a false default, or the Classic card flashes before the grid.
  if (filesLoading) {
    return (
      <Card className="grid place-items-center p-10 text-sm text-muted-foreground">
        Loading policy…
      </Card>
    );
  }

  // Classic projects don't compose a cross-role grid — each agent enforces its
  // own policy. Point at the editor rather than show an empty grid.
  if (!modular) {
    return (
      <Card className="grid place-items-center gap-2 p-10 text-center text-sm text-muted-foreground">
        <span>
          Classic project —{" "}
          <span className="font-mono text-foreground">{agent.name}</span>{" "}
          enforces its own policy, with no composed roles × tools grid.
        </span>
        <Link to={policyHref} className="text-primary hover:underline">
          Open in Policies →
        </Link>
      </Card>
    );
  }

  return (
    <Card className="overflow-hidden p-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-4 py-3">
        <h2 className="text-body font-semibold">
          How each role may use <span className="font-mono">{agent.name}</span>
          &rsquo;s tools
        </h2>
        <span className="text-meta text-muted-foreground">
          resolved by the policy engine
        </span>
        <Link
          to={policyHref}
          className="ml-auto text-meta text-primary hover:underline"
        >
          Edit in Policies →
        </Link>
      </div>
      <div className="flex h-[440px] flex-col">
        <ResolvedGrid matrix={matrix} onPick={() => navigate(policyHref)} />
      </div>
      {fileNames.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-2.5 text-meta text-muted-foreground">
          <span>Governed by</span>
          {fileNames.map((f) => (
            <Link
              key={f}
              to={policyHref}
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground hover:bg-accent"
            >
              {f}
            </Link>
          ))}
        </div>
      )}
    </Card>
  );
}

/** Decision log: the audit log pre-scoped to this agent (24h). Reuses the full
 * audit table + detail drawer; the drawer's "Filter to…" footer is hidden —
 * every row here is already this agent. */
function AgentDecisions({ agent }: { agent: AgentManifestView }) {
  const { projectId } = useProjectScoped();
  const [limit, setLimit] = useState(50);
  const [sel, setSel] = useState<AuditDecisionRow | null>(null);

  const listFilters = { agent: agent.name, window: "24h" as const, limit };
  const listQ = useQuery({
    queryKey: ["audit", "list", projectId, listFilters],
    enabled: !!projectId,
    queryFn: () => api.listAuditDecisions(listFilters, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  // Siblings in the selected decision's session — the drawer's "Same session".
  const relatedQ = useQuery({
    queryKey: ["audit", "session", projectId, sel?.session_id],
    enabled: !!sel?.session_id && !!projectId,
    queryFn: () =>
      api.listAuditDecisions(
        { window: "24h", session_id: sel!.session_id, limit: 12 },
        projectId as string,
      ),
  });
  const related = (relatedQ.data?.rows ?? [])
    .filter((r) => r.event_id !== sel?.event_id)
    .slice(0, 6);

  // Exports the loaded rows only, like the full audit log.
  const exportJsonl = () => {
    const rows = listQ.data?.rows ?? [];
    if (!rows.length) return;
    const blob = new Blob([rows.map((r) => JSON.stringify(r)).join("\n")], {
      type: "application/x-ndjson",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${agent.name}-decisions.jsonl`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-meta text-muted-foreground">
        <span>
          Decisions for{" "}
          <span className="font-mono text-foreground">{agent.name}</span> · last
          24h
        </span>
        <Link
          to={auditUrl({ agent: agent.name, range: "24h" })}
          className="ml-auto text-primary hover:underline"
        >
          Open in full audit log →
        </Link>
      </div>
      <EventsTable
        rows={listQ.data?.rows ?? []}
        total={listQ.data?.total ?? 0}
        onSelect={setSel}
        selectedId={sel?.event_id}
        onLoadMore={() => setLimit((l) => Math.min(l + 50, 200))}
        // isPlaceholderData is true only while a *larger* page loads (the key
        // changed) — not during the 30s background poll of the same page.
        loadingMore={listQ.isPlaceholderData}
        onExport={exportJsonl}
      />
      <DetailDrawer
        event={sel}
        related={related}
        projectId={projectId}
        onClose={() => setSel(null)}
        onSelect={setSel}
      />
    </div>
  );
}

function AgentHeader({
  agent,
  canManageBans,
}: {
  agent: AgentManifestView;
  canManageBans: boolean;
}) {
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-0 flex-1 basis-[320px]">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="font-mono text-title-sm font-semibold">
              {agent.name}
            </h1>
            <Badge variant="outline" className="font-mono text-[11px]">
              {agent.version !== null ? `v${agent.version}` : "not registered"}
            </Badge>
          </div>
          {agent.manifest?.description?.trim() && (
            <p className="mt-1.5 max-w-[640px] text-sm text-muted-foreground">
              {agent.manifest.description.trim()}
            </p>
          )}
          <dl className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-meta text-muted-foreground">
            <div>
              <dt className="inline">Model </dt>
              <dd className="inline font-mono text-foreground">
                {agent.manifest?.model?.trim() || "—"}
              </dd>
            </div>
            <div>
              <dt className="inline">Framework </dt>
              <dd className="inline font-mono text-foreground">
                {agent.manifest?.framework?.trim() || "—"}
              </dd>
            </div>
            <div>
              <dt className="inline">Updated </dt>
              <dd className="inline text-foreground">
                {formatDate(agent.updated_at)}
              </dd>
            </div>
          </dl>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button asChild variant="outline" size="sm" className="gap-1.5">
            <Link to={`/policies?agent=${encodeURIComponent(agent.name)}`}>
              <ShieldCheck className="size-3.5" />
              Edit policy
            </Link>
          </Button>
          <Button asChild variant="outline" size="sm" className="gap-1.5">
            <Link to={auditUrl({ agent: agent.name, range: "24h" })}>
              <ScrollText className="size-3.5" />
              View decisions
            </Link>
          </Button>
          {canManageBans && (
            <Button
              asChild
              variant="outline"
              size="sm"
              className="gap-1.5 text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              <Link to={`/bans?ban_agent=${encodeURIComponent(agent.name)}`}>
                <Ban className="size-3.5" />
                Ban agent
              </Link>
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
