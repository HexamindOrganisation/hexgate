import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Ban, ChevronRight, ScrollText, ShieldCheck } from "lucide-react";

import { api, type AgentManifestView } from "@/lib/api";
import { usePolicyFiles, usePolicyMatrix } from "@/lib/policy_files";
import { ENTRY_FILE } from "@/lib/file_tree";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { useAuditSummary24h } from "@/lib/audit-summary";
import { auditUrl } from "@/lib/audit-filters";
import { formatDate } from "@/lib/format";
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
      <AgentHeader
        agent={agent}
        activity={activity}
        activityError={summary.isError && !summary.data}
        canManageBans={canManageBans}
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

/* NOTE: AgentHeader and the roleModes memo mirror Agents.tsx on purpose, for
   now — H1b makes /agents list-only (dropping its inline card) and H2 replaces
   this header with the graph-hero Overview, so a shared extraction here would
   be reworked immediately. Kept parallel until then. */
function AgentHeader({
  agent,
  activity,
  activityError,
  canManageBans,
}: {
  agent: AgentManifestView;
  activity: Activity | null;
  activityError: boolean;
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
      {activityError ? (
        <p className="mt-4 border-t border-border/60 pt-4 text-meta text-muted-foreground">
          Couldn't load the last 24 hours of activity.
        </p>
      ) : (
        activity && (
          <div className="mt-4 grid grid-cols-3 border-t border-border/60 pt-4 text-meta">
            {(
              [
                { label: "Calls · 24h", value: activity.all, className: "" },
                {
                  label: "Denied",
                  value: activity.deny,
                  className: "text-deny",
                },
                {
                  label: "Sent for approval",
                  value: activity.needs_approval,
                  className: "text-approval",
                },
              ] satisfies { label: string; value: number; className: string }[]
            ).map((stat) => (
              <div key={stat.label}>
                <div className="text-muted-foreground">{stat.label}</div>
                <div
                  className={cn(
                    "mt-1 text-[20px] font-semibold tabular-nums",
                    stat.className,
                  )}
                >
                  {stat.value.toLocaleString()}
                </div>
              </div>
            ))}
          </div>
        )
      )}
    </Card>
  );
}
