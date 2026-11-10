import { useMemo } from "react";
import {
  Link,
  NavLink,
  Outlet,
  useOutletContext,
  useParams,
} from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Ban, ChevronRight, ScrollText, ShieldCheck } from "lucide-react";

import {
  ApiError,
  api,
  type AgentManifestView,
  type PolicyGraph,
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
import { auditUrl } from "@/lib/audit-filters";
import { agentReachCounts, agentSubgraph } from "@/lib/agent-graph";
import { formatDate, pluralise } from "@/lib/format";
import { PolicyFlow } from "@/components/policy_files/PolicyGraphDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { cn } from "@/lib/utils";
import { ManifestView } from "./Agents";

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
  const matrix = usePolicyMatrix(
    scope.projectId,
    agent?.name ?? "",
    modular && !!agent,
    toolNames,
  ).data;
  const roleModes = useMemo(
    () => (matrix ? roleModesOf(matrix) : undefined),
    [matrix],
  );

  // The agent's local graph (its roles, tools and reached agents), filtered
  // from the project graph — no backend change. Every project has one (a
  // classic project's comes from its tier store): a 422 means the modules
  // don't compose, a 404 that the project isn't visible to the user.
  const graphQuery = usePolicyGraph(scope.projectId, undefined, !!agent);
  const graph = graphQuery.data;
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

  const canManageBans = useCanManageBans();

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
      <HubTabs resources={resourceCount(agent)} />
      <Outlet
        context={
          {
            agent,
            roleModes,
            activity,
            activityError: summary.isError && !summary.data,
            activityStale: summary.isError && !!summary.data,
            subgraph,
            // The previous project's graph, kept while this one loads, isn't
            // this agent's: show it as loading.
            graphLoading: graphQuery.isLoading || graphQuery.isPlaceholderData,
            graphError,
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
  activity: { all: number; deny: number; needs_approval: number } | null;
  /** The 24h summary never loaded: "—" in the strip and a note. */
  activityError: boolean;
  /** A later poll failed: the last counts stay, marked as such. */
  activityStale: boolean;
  subgraph: PolicyGraph | null;
  graphLoading: boolean;
  graphError: GraphError | null;
}

export function AgentOverviewTab() {
  return <AgentOverview {...useOutletContext<HubContext>()} />;
}

/** The Resources tab — the manifest's tools, skills and guards. */
export function AgentResourcesTab() {
  const ctx = useOutletContext<HubContext>();
  return <ManifestView agent={ctx.agent} roleModes={ctx.roleModes} />;
}

/** Tools + skills, or null for an agent that never registered: a 0 would
 * assert what the "not registered yet" copy refuses to. */
function resourceCount(agent: AgentManifestView): number | null {
  const m = agent.manifest;
  if (!m) return null;
  return (m.tools?.length ?? 0) + (m.skills?.length ?? 0);
}

function HubTabs({ resources }: { resources: number | null }) {
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
        {resources !== null && (
          <Badge variant="outline" className="ml-1 font-mono text-[11px]">
            {resources}
          </Badge>
        )}
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
      <div className="px-4 py-2.5 text-meta text-muted-foreground">
        What <span className="font-mono text-foreground">{agent.name}</span> can
        reach{hasGraph && ` — ${caption || "nothing it may use yet"}`}
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
          {graphPlaceholder(graphLoading, graphError, agent.name)}
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
): string {
  if (loading) return "Loading graph…";
  if (error === "compose")
    return "The policy modules don't compose, so there's no graph: fix the lints in Policies.";
  if (error === "gone")
    return "You don't have access to this project's policy any more.";
  if (error === "load") return "Couldn't load the graph. Refresh to retry.";
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

/* NOTE: AgentHeader mirrors the inline card in Agents.tsx on purpose: H1b
   makes /agents list-only and drops that card, so a shared extraction now
   would be reworked immediately. */
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
