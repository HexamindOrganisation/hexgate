import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
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
import { ENTRY_FILE } from "@/lib/file_tree";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { useAuditSummary24h } from "@/lib/audit-summary";
import { auditUrl } from "@/lib/audit-filters";
import { agentSubgraph } from "@/lib/agent-graph";
import { formatDate, pluralise } from "@/lib/format";
import { PolicyFlow } from "@/components/policy_files/PolicyGraphDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { cn } from "@/lib/utils";
import { ManifestView, type RoleMode } from "./Agents";

type Activity = { all: number; deny: number; needs_approval: number };

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
  const activity: Activity | null =
    agent && summary.data
      ? (summary.data.by_agent.find((r) => r.key === agent.name) ?? {
          all: 0,
          deny: 0,
          needs_approval: 0,
        })
      : null;

  // Roles x tools for this agent, as the engine tabulates them. Only projects
  // with a policy.yaml compose; others have no matrix.
  const policyFiles = usePolicyFiles(scope.projectId);
  const modular = !!policyFiles.data?.some((f) => f.name === ENTRY_FILE);
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
      <AgentOverview
        agent={agent}
        activity={activity}
        activityError={summary.isError && !summary.data}
        subgraph={subgraph}
        graphLoading={graphQuery.isLoading}
        graphError={graphQuery.isError && !graph404}
      />
      <ManifestView agent={agent} roleModes={roleModes} />
    </div>
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
  const tools = agent.manifest?.tools?.length ?? 0;
  const skills = agent.manifest?.skills?.length ?? 0;
  const kpis: { label: string; value: string; className?: string }[] = [
    { label: "Calls · 24h", value: num(activity?.all) },
    { label: "Denied", value: num(activity?.deny), className: "text-deny" },
    {
      label: "Approvals",
      value: num(activity?.needs_approval),
      className: "text-approval",
    },
    { label: "Resources", value: `${tools + skills}` },
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
      ) : subgraph && subgraph.nodes.length === 1 ? (
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

/* NOTE: AgentHeader and the roleModes memo mirror Agents.tsx on purpose, for
   now — H1b makes /agents list-only (dropping its inline card) and H2 replaces
   this header with the graph-hero Overview, so a shared extraction here would
   be reworked immediately. Kept parallel until then. */
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
