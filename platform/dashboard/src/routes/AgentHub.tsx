import { useMemo, useState } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useNavigate,
  useOutletContext,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Ban,
  ChevronRight,
  Maximize2,
  Network,
  ScrollText,
  ShieldCheck,
} from "lucide-react";

import {
  ApiError,
  api,
  type AgentManifestView,
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
import { isModular } from "@/lib/file_tree";
import { roleModesOf, type RoleMode } from "@/lib/role-modes";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { useAgentActivity24h } from "@/lib/audit-summary";
import { auditUrl, PAGE_SIZE } from "@/lib/audit-filters";
import { useDecisionLog } from "@/lib/decision-log";
import {
  agentReachCounts,
  agentReachEdges,
  agentSubgraph,
} from "@/lib/agent-graph";
import { rangeSearch } from "@/lib/range-params";
import { formatDate, pluralise } from "@/lib/format";
import {
  PolicyFlow,
  PolicyGraphDialog,
  VerdictTag,
} from "@/components/policy_files/PolicyGraphDialog";
import { ResolvedGrid } from "@/components/policy_files/InspectorTabs";
import { DetailDrawer } from "@/components/audit/drawer";
import { EventsTable } from "@/components/audit/pieces";
import { ManifestView } from "@/components/agents/ManifestView";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { cn } from "@/lib/utils";
import { PlaygroundLive } from "./Playground";
import { usePlaygroundServedAgent } from "@/lib/playground";

/** Tools + skills + handoff sub-agents (an agent-as-tool sub-agent is already
 * one of its tools), or null for an agent that never registered: a 0 would
 * assert what the "not registered yet" copy refuses to. */
function resourceCount(agent: AgentManifestView): number | null {
  const m = agent.manifest;
  if (!m) return null;
  const tools = new Set(m.tools?.map((t) => t.name));
  const subs = new Set(
    m.subagents?.map((s) => s.name).filter((n) => !tools.has(n)),
  );
  return tools.size + (m.skills?.length ?? 0) + subs.size;
}

/** A sub-agent from the manifest, with its policy edge when the project has a
 * graph, and its own 24h call count (null until the summary lands). */
type SubAgentRow = SubagentRef & {
  edge: PolicyGraphEdge | null;
  /** No edge names it: denied once the policy declares reach that gates its
   * via (closed world, R-AGENT-002), else never checked as a reach. An
   * agent-as-tool is gated only by tool reach; without any, it's checked
   * under its tool name ("as-tool"). Null without a graph. */
  unnamed: "denied" | "ungated" | "as-tool" | null;
  /** Registered in this project, so its hub exists. */
  registered: boolean;
  calls: number | null;
};

/** `/agents/:name`: everything about one agent on its own page. The header
 * stays above the tabs; each tab renders in the `Outlet` from `HubContext`. */
export function AgentHub() {
  const { name } = useParams<{ name: string }>();
  const scope = useProjectScoped();

  const manifests = useQuery({
    queryKey: ["agent-manifests", scope.projectId],
    queryFn: () => api.listAgentManifests(scope.projectId as string),
    enabled: !!scope.projectId,
  });
  const agent = manifests.data?.find((m) => m.name === name) ?? null;

  // 24h activity, by the same rule as Home and /agents.
  const { summary, activityOf } = useAgentActivity24h(scope.projectId);
  const activity = agent ? activityOf(agent.name) : null;

  // Roles x tools for this agent, as the engine tabulates them. Only projects
  // with a policy.yaml compose; others have no matrix.
  const policyFiles = usePolicyFiles(scope.projectId);
  const modular = isModular(policyFiles.data);
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
  const roleModes = useMemo(
    () => (matrix ? roleModesOf(matrix) : undefined),
    [matrix],
  );

  // The agent's local graph (its roles, tools and reached agents), filtered
  // from the project graph — no backend change. A classic project's comes
  // from its tier store, usually empty: its agents follow their own policy
  // file. A 422 means the modules don't compose, a 404 that the project isn't
  // visible to the user.
  const graphQuery = usePolicyGraph(scope.projectId, undefined, !!agent);
  const [graphOpen, setGraphOpen] = useState(false);
  // Not a prior project's graph, kept while this one loads.
  const graph = graphQuery.isPlaceholderData ? undefined : graphQuery.data;
  const status =
    graphQuery.error instanceof ApiError ? graphQuery.error.status : null;
  const graphError: GraphError | null = !graphQuery.isError
    ? null
    : status === 422
      ? "compose"
      : status === 404
        ? "gone"
        : "load";
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
    // The SDK gates an as-tool call as a reach only when the policy declares
    // tool reach; a handoff engages on any reach.
    const toolReach =
      !!edges && [...edges.keys()].some((k) => k.endsWith(":tool"));
    const unnamedOf = (via: SubagentRef["via"]): SubAgentRow["unnamed"] => {
      if (!edges) return null;
      if (via === "tool") return toolReach ? "denied" : "as-tool";
      return edges.size > 0 ? "denied" : "ungated";
    };
    return unique.map((r) => {
      const edge = edges?.get(`${r.name}:${r.via}`) ?? null;
      return {
        ...r,
        edge,
        unnamed: edge ? null : unnamedOf(r.via),
        registered: names.has(r.name),
        calls: activityOf(r.name)?.all ?? null,
      };
    });
  }, [agent, manifests.data, subgraph, activityOf]);

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
      <AgentHeader
        agent={agent}
        canManageBans={canManageBans}
        classic={!!policyFiles.data && !modular}
      />
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
            activity,
            activityError: summary.isError && !summary.data,
            activityStale: summary.isError && !!summary.data,
            subgraph,
            subAgents,
            // One dialog for every entry point; only when there's more than
            // the agent's own node to draw.
            openGraph:
              scope.projectId && subgraph && subgraph.nodes.length > 1
                ? () => setGraphOpen(true)
                : null,
            // The previous project's graph, kept while this one loads, isn't
            // this agent's: show it as loading.
            graphLoading: graphQuery.isLoading || graphQuery.isPlaceholderData,
            graphError,
            classic: !!policyFiles.data && !modular,
            playgroundLive,
            servedAgent: served.agentName,
            serveOnline: served.online,
          } satisfies HubContext
        }
      />
      {graphOpen && scope.projectId && (
        <PolicyGraphDialog
          open
          onOpenChange={setGraphOpen}
          projectId={scope.projectId}
          roleNames={graph?.roles ?? []}
          agent={agent.name}
        />
      )}
    </div>
  );
}

/** Shared state the hub computes once and its tabs read via the Outlet. */
export interface HubContext {
  agent: AgentManifestView;
  roleModes?: (tool: string) => RoleMode[];
  /** Engine-tabulated roles x tools for this agent: null on error, undefined
   * while loading and on a classic project (the query is off). */
  matrix: PolicyMatrix | undefined | null;
  activity: { all: number; deny: number; needs_approval: number } | null;
  /** The 24h summary never loaded: "—" in the strip and a note. */
  activityError: boolean;
  /** A later poll failed: the last counts stay, marked as such. */
  activityStale: boolean;
  subgraph: PolicyGraph | null;
  /** The manifest's sub-agents; null when it records none. */
  subAgents: SubAgentRow[] | null;
  /** Opens the agent's graph dialog; null when there's no graph to show. */
  openGraph: (() => void) | null;
  graphLoading: boolean;
  graphError: GraphError | null;
  /** No policy.yaml: the agent is governed by its own policy file. */
  classic: boolean;
  /** This agent is the one currently served, so its Playground tab is live. */
  playgroundLive: boolean;
  /** The agent the serve relay currently exposes (any), or null if none. */
  servedAgent: string | null;
  /** A serve is attached, even if it hasn't announced its agent yet. */
  serveOnline: boolean;
}

export function AgentOverviewTab() {
  return <AgentOverview {...useOutletContext<HubContext>()} />;
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
          subAgents={ctx.subAgents}
          unregistered={ctx.agent.manifest === null}
          onOpenGraph={ctx.openGraph}
          // agentSubgraph is empty (not null) when the agent isn't in the graph.
          inGraph={!!ctx.subgraph && ctx.subgraph.nodes.length > 0}
          callsError={ctx.activityError}
          graphLoading={ctx.graphLoading}
          graphError={ctx.graphError}
          callsStale={ctx.activityStale}
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
  subAgents,
  unregistered,
  onOpenGraph,
  inGraph,
  callsError,
  graphLoading,
  graphError,
  callsStale,
}: {
  subAgents: SubAgentRow[] | null;
  unregistered: boolean;
  /** Opens the agent's graph dialog; null when there's no graph to show. */
  onOpenGraph: (() => void) | null;
  /** The project's policy graph has this agent, so rows carry verdicts. */
  inGraph: boolean;
  callsError: boolean;
  graphLoading: boolean;
  graphError: GraphError | null;
  /** A later 24h poll failed: the call counts are the last loaded. */
  callsStale: boolean;
}) {
  const notes = [
    graphLoading && "Loading verdicts…",
    graphError && reachGraphNote(graphError, inGraph),
    callsStale && "Couldn't refresh; call counts are the last loaded.",
  ].filter(Boolean);
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
        {onOpenGraph && (
          <button
            type="button"
            onClick={onOpenGraph}
            className="ml-auto text-xs text-muted-foreground hover:text-foreground"
          >
            Open graph →
          </button>
        )}
      </div>
      {notes.length > 0 && subAgents && subAgents.length > 0 && (
        <p className="border-b border-border px-5 py-2 text-meta text-muted-foreground">
          {notes.join(" ")}
        </p>
      )}
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

/** Why the reach verdicts are missing or may be stale. */
function reachGraphNote(error: GraphError, cached: boolean): string {
  if (cached)
    return error === "compose"
      ? "The policy modules don't compose any more; verdicts are from the last graph loaded."
      : "Couldn't refresh the graph; verdicts are from the last one loaded.";
  if (error === "compose")
    return "The policy modules don't compose, so there are no verdicts: fix the lints in Policies.";
  if (error === "gone")
    return "You don't have access to this project's policy any more.";
  return "Couldn't load the graph, so there are no verdicts.";
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
      {sub.unnamed === "as-tool" && (
        <span
          className="ml-auto text-[11px] text-muted-foreground"
          title="Its policy declares no agent-as-tool reach, so this call is checked under its tool name."
        >
          checked as a tool
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

export function AgentPoliciesTab() {
  const ctx = useOutletContext<HubContext>();
  return <AgentPolicies agent={ctx.agent} matrix={ctx.matrix} />;
}

export function AgentDecisionsTab() {
  const ctx = useOutletContext<HubContext>();
  const { projectId } = useProjectScoped();
  // Keyed, so another agent's (or project's) page size doesn't carry over.
  return (
    <AgentDecisions key={`${projectId}:${ctx.agent.name}`} agent={ctx.agent} />
  );
}

/** The Playground tab — a live chat with THIS agent, embedded in the hub. The
 * relay is project-scoped (one `hexgate serve` at a time), so the tab only
 * shows a session when this agent is the one being served; otherwise it's a
 * "not served" card that says how to serve it (the nav tab is muted). */
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
  resources: number | null;
  playgroundLive: boolean;
}) {
  const [params] = useSearchParams();
  const range = rangeSearch(params);
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
      {/* Stats and Usage share the URL range, so switching keeps it. */}
      <NavLink to={`stats${range}`} className={cls}>
        Stats
      </NavLink>
      <NavLink to={`usage${range}`} className={cls}>
        Usage
      </NavLink>
      <NavLink to="resources" className={cls}>
        Resources
        {resources !== null && (
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {resources}
          </Badge>
        )}
      </NavLink>
      <NavLink to="policies" className={cls}>
        Policies
      </NavLink>
      {/* Always a link, so the not-served card that teaches `hexgate serve`
          stays reachable; muted while another agent (or none) is served. */}
      <NavLink
        to="playground"
        className={(p) => cn(cls(p), !playgroundLive && "opacity-60")}
        title={
          playgroundLive
            ? undefined
            : "Not served: run `hexgate serve` for this agent to test it here"
        }
      >
        Playground
      </NavLink>
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

type GraphError = "compose" | "gone" | "load";

/** Overview: the 24h strip, then the agent's graph as the centrepiece. */
function AgentOverview({
  agent,
  activity,
  activityError,
  activityStale,
  subgraph,
  graphLoading,
  graphError,
  classic,
  openGraph,
}: Omit<HubContext, "roleModes">) {
  const num = (n: number | null | undefined) => n?.toLocaleString() ?? "—";
  const kpis: { label: string; value: string; className?: string }[] = [
    { label: "Calls · 24h", value: num(activity?.all) },
    { label: "Denied", value: num(activity?.deny), className: "text-deny" },
    {
      label: "Sent for approval",
      value: num(activity?.needs_approval),
      className: "text-approval",
    },
    { label: "Resources", value: num(resourceCount(agent)) },
  ];

  // What the agent itself may use: its own outbound edges, never a denied one.
  const reach = subgraph?.nodes.length ? agentReachCounts(subgraph) : null;
  const caption = reach
    ? [
        reach.tools && pluralise(reach.tools, "tool"),
        reach.mcpTools && pluralise(reach.mcpTools, "MCP tool"),
        reach.agents && pluralise(reach.agents, "agent"),
      ]
        .filter(Boolean)
        .join(", ")
    : "";

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
      {(activityError || activityStale) && (
        <div className="border-b border-border px-4 py-2 text-meta text-muted-foreground">
          {activityError
            ? "Couldn't load the last 24 hours of activity."
            : "Couldn't refresh; these are the last counts loaded."}
        </div>
      )}
      <div className="flex items-center gap-2 px-4 py-2.5 text-meta text-muted-foreground">
        <span>
          What <span className="font-mono text-foreground">{agent.name}</span>{" "}
          can reach{hasGraph && ` — ${caption || "nothing it may use yet"}`}
        </span>
        {hasGraph && openGraph && (
          <button
            type="button"
            onClick={openGraph}
            title="Open the graph in a larger view"
            className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-foreground transition-colors hover:bg-accent"
          >
            <Maximize2 className="size-3" />
            Expand
          </button>
        )}
      </div>
      {hasGraph && graphError && (
        // A refetch failed over the cached graph: it may predate the policy.
        <div className="border-t border-border px-4 py-2 text-meta text-muted-foreground">
          {graphError === "compose"
            ? "The policy modules don't compose any more"
            : "Couldn't refresh the graph"}
          ; the graph below is the last one loaded.
        </div>
      )}
      {hasGraph ? (
        <div className="h-[420px] border-t border-border">
          <PolicyFlow graph={subgraph} embedded />
        </div>
      ) : (
        <Placeholder>
          {graphPlaceholder(graphLoading, graphError, agent.name, classic)}
        </Placeholder>
      )}
    </Card>
  );
}

/** Why there's no graph to show. */
function graphPlaceholder(
  loading: boolean,
  error: GraphError | null,
  name: string,
  classic: boolean,
): string {
  if (loading) return "Loading graph…";
  if (error === "compose")
    return "The policy modules don't compose, so there's no graph: fix the lints in Policies.";
  if (error === "gone")
    return "You don't have access to this project's policy any more.";
  if (error === "load") return "Couldn't load the graph. Refresh to retry.";
  if (classic)
    return `${name} is governed by its own policy file. The graph appears once the project uses policy.yaml or role bindings.`;
  // The graph comes from the policy, not the manifest.
  return `Nothing in the policy maps roles, tools or reach to ${name} yet.`;
}

function Placeholder({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid h-40 place-items-center border-t border-border px-6 text-center text-meta text-muted-foreground">
      {children}
    </div>
  );
}

/** Policies: the engine-resolved roles x tools grid for this agent, plus the
 * project's policy files. Read-only here: editing stays on the Policies page. */
/** A classic agent's own policy file, read-only. */
function ClassicPolicy({ agent }: { agent: string }) {
  const { projectId } = useProjectScoped();
  const q = useQuery({
    queryKey: ["agent", projectId, agent],
    queryFn: () => api.getAgent(agent, projectId as string),
    enabled: !!projectId,
  });
  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b border-border px-4 py-3 text-meta text-muted-foreground">
        Classic project:{" "}
        <span className="font-mono text-foreground">{agent}</span> enforces its
        own policy file, with no composed roles × tools grid.
      </div>
      {q.data ? (
        <pre className="max-h-[440px] overflow-auto px-4 py-3 font-mono text-[12px] text-foreground">
          {q.data.policy_yaml.trim() || "# empty policy"}
        </pre>
      ) : (
        <p className="px-4 py-6 text-center text-sm text-muted-foreground">
          {q.isError ? "Couldn't load its policy file." : "Loading policy…"}
        </p>
      )}
    </Card>
  );
}

function AgentPolicies({
  agent,
  matrix,
}: {
  agent: AgentManifestView;
  matrix: PolicyMatrix | undefined | null;
}) {
  const navigate = useNavigate();
  const { projectId } = useProjectScoped();
  // A cached hit: the hub already loaded the files.
  const files = usePolicyFiles(projectId);
  const kind = files.data
    ? isModular(files.data)
      ? "modular"
      : "classic"
    : files.isError
      ? "error"
      : "loading";

  if (kind === "loading" || kind === "error") {
    return (
      <Card className="grid place-items-center p-10 text-sm text-muted-foreground">
        {kind === "loading"
          ? "Loading policy…"
          : "Couldn't load this project's policy files. Try refreshing the page."}
      </Card>
    );
  }

  // Classic projects don't compose a cross-role grid: each agent enforces its
  // own policy file, which /policies doesn't show (its only action, adding
  // policy.yaml, flips the project to compose). Show that file here.
  if (kind === "classic") return <ClassicPolicy agent={agent.name} />;

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
          to="/policies"
          className="ml-auto text-meta text-primary hover:underline"
        >
          Edit in Policies →
        </Link>
      </div>
      <div className="flex h-[440px] flex-col">
        <ResolvedGrid
          matrix={matrix}
          errorText="Couldn't load the grid. Try refreshing the page."
          onPick={() => navigate("/policies")}
        />
      </div>
      {!!files.data?.length && (
        <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-2.5 text-meta text-muted-foreground">
          <span>Project policy files</span>
          {files.data.map((f) => (
            <span
              key={f.name}
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-foreground"
            >
              {f.name}
            </span>
          ))}
        </div>
      )}
    </Card>
  );
}

/** Decision log: the audit log pre-scoped to this agent (24h), with the full
 * audit table and drawer. The drawer's "Filter to…" footer is hidden: every
 * row here is already this agent. */
function AgentDecisions({ agent }: { agent: AgentManifestView }) {
  const { projectId } = useProjectScoped();
  const [limit, setLimit] = useState(PAGE_SIZE);
  const {
    listQ,
    page,
    loadMore,
    loadingMore,
    sel,
    setSel,
    clearSel,
    related,
    exportJsonl,
  } = useDecisionLog(
    projectId,
    { agent: agent.name, window: "24h", limit },
    setLimit,
    `${agent.name}-decisions.jsonl`,
  );
  const failed = listQ.isError && !page;

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
      {listQ.isError && page && (
        <p className="text-meta text-muted-foreground">
          Couldn't refresh; these are the last decisions loaded.
        </p>
      )}
      <EventsTable
        rows={page?.rows ?? []}
        total={page?.total ?? 0}
        onSelect={setSel}
        selectedId={sel?.event_id}
        onLoadMore={loadMore}
        loadingMore={loadingMore}
        onExport={exportJsonl}
        emptyText={
          listQ.isPending
            ? "Loading decisions…"
            : failed
              ? "Couldn't load this agent's decisions. Try refreshing the page."
              : "No decisions in the last 24h."
        }
      />
      <DetailDrawer
        event={sel}
        related={related}
        projectId={projectId}
        onClose={clearSel}
        onSelect={setSel}
      />
    </div>
  );
}

function AgentHeader({
  agent,
  canManageBans,
  classic,
}: {
  agent: AgentManifestView;
  canManageBans: boolean;
  /** Its policy is its own file, not in /policies: link to the hub's tab. */
  classic: boolean;
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
            <Link
              to={
                classic
                  ? `/agents/${encodeURIComponent(agent.name)}/policies`
                  : `/policies?agent=${encodeURIComponent(agent.name)}`
              }
            >
              <ShieldCheck className="size-3.5" />
              {classic ? "Policy" : "Edit policy"}
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
