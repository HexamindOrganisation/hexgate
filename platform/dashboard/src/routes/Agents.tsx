import { useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, X } from "lucide-react";
import { Link, Navigate, useSearchParams } from "react-router-dom";

import { agentReachMap } from "@/lib/agent-graph";
import { usePolicyGraph } from "@/lib/policy_files";
import { ApiError, api } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { Badge } from "@/components/ui/badge";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { DocsLink } from "@/components/DocsLink";
import { DOC_PATHS } from "@/lib/docs";
import { Card } from "@/components/ui/card";
import { SearchField } from "@/components/ui/search-field";
import { Button } from "@/components/ui/button";
import { AgentFilterDialog } from "@/components/agents/AgentFilterDialog";
import {
  type AgentFacts,
  type AgentFilters,
  NO_FILTERS,
  activeCount,
  matches,
  ACTIVITY_LABEL,
  rolesThatRun,
} from "@/lib/agent-filters";
import { PageHeader } from "@/components/ui/page-header";
import { StatStrip, type Stat } from "@/components/ui/stat-strip";
import { AGENT_LIST_CAP, agentHubPath } from "@/lib/agent-paths";
import { denyRatePct, useAgentActivity24h } from "@/lib/audit-summary";
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * /agents — the agent list, the workspace's landing for agents.
 *
 * One row per registered agent; each opens its hub at /agents/:name,
 * where the manifest, graph, resolved policy and decisions live. A row
 * carries just enough to choose between agents: registration status,
 * model / framework, tool & skill counts, agent-to-agent delegation
 * (reaches / called by), and 24h call / denial volume.
 *
 * Editing happens elsewhere: policy authoring in /policies, manifest
 * registration through the SDK's ``hexgate register``.
 */

export function AgentsPage() {
  const scope = useProjectScoped();
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<AgentFilters>(NO_FILTERS);
  const [filtering, setFiltering] = useState(false);
  // Filters belong to a project: another project starts unfiltered.
  const [filtersFor, setFiltersFor] = useState(scope.projectId);
  if (filtersFor !== scope.projectId) {
    setFiltersFor(scope.projectId);
    setFilters(NO_FILTERS);
    setQuery("");
  }
  const manifests = useQuery({
    queryKey: ["agent-manifests", scope.projectId],
    queryFn: () => api.listAgentManifests(scope.projectId as string),
    enabled: !!scope.projectId,
  });
  // Below the cap the toolbar is gone: drop its filters rather than keep
  // ones nobody can see, which would come back when the list grows.
  if (
    (activeCount(filters) > 0 || query) &&
    manifests.data &&
    manifests.data.length <= AGENT_LIST_CAP
  ) {
    setFilters(NO_FILTERS);
    setQuery("");
  }
  // 24h activity per agent; shares Home's cache entry.
  const { summary, activityOf } = useAgentActivity24h(scope.projectId);
  // Agent-to-agent delegation, from the policy graph (a classic project's
  // comes from its tier store): who each agent may reach and who may reach
  // it. Without a graph the rows omit it, and a note says why.
  const graph = usePolicyGraph(scope.projectId);
  const graphFailed = graph.isError && !graph.data;
  const rowId = useId();
  const reachByAgent = useMemo(
    // Not a prior project's graph kept while this one loads.
    () =>
      graph.data && !graph.isPlaceholderData
        ? agentReachMap(
            graph.data,
            (manifests.data ?? []).map((a) => a.name),
          )
        : new Map(),
    [graph.data, graph.isPlaceholderData, manifests.data],
  );
  // Which roles run each agent: one pass per graph, not per summary poll.
  const runBy = useMemo(
    () =>
      rolesThatRun(
        graph.data && !graph.isPlaceholderData ? graph.data : undefined,
        (manifests.data ?? []).map((a) => a.name),
      ),
    [graph.data, graph.isPlaceholderData, manifests.data],
  );
  // A prior project's graph, kept while this one loads, isn't this one's.
  const graphKnown = !!graph.data && !graph.isPlaceholderData;
  // What the filter reads off each agent.
  const facts = useMemo<AgentFacts[]>(
    () =>
      (manifests.data ?? []).map((a) => {
        const reach = reachByAgent.get(a.name);
        return {
          name: a.name,
          registered: !!a.manifest,
          framework: a.manifest?.framework?.trim() || null,
          activity: activityOf(a.name),
          reaches: !!reach?.reaches.length,
          calledBy: !!reach?.calledBy.length,
          runBy: runBy.get(a.name) ?? null,
          graphKnown,
        };
      }),
    [manifests.data, reachByAgent, activityOf, runBy, graphKnown],
  );

  // The 24h summary never loaded, so every count reads "—"; or a later poll
  // (of it or the agent list) failed, so what's shown is the last loaded.
  const summaryFailed = summary.isError && !summary.data;
  const stale =
    (summary.isError && !!summary.data) ||
    (manifests.isError && !!manifests.data) ||
    (graph.isError && !!graph.data);

  // Portfolio KPIs across the project's agents, memoised
  // (the 24h summary polls, so don't re-derive every render). Volume reads
  // "—" (never a fake "0") until the summary loads or on error; the count
  // comes from the manifest list. No live/offline tile — agent connection
  // status isn't a signal the backend exposes yet.
  const kpis = useMemo<Stat[]>(() => {
    const list = manifests.data ?? [];
    const totals = summary.data?.totals;
    const vol = (n?: number) =>
      summary.data ? (n ?? 0).toLocaleString() : "—";
    const registered = list.filter((a) => a.manifest).length;
    const unregistered = list.length - registered;
    const denyRate = totals ? denyRatePct(totals.deny, totals.all) : null;
    const topTool = (field: "deny" | "needs_approval") =>
      (summary.data?.by_tool ?? [])
        .filter((r) => r[field] > 0)
        .sort((a, b) => b[field] - a[field])[0] ?? null;
    const topDeny = topTool("deny");
    const topApproval = topTool("needs_approval");
    return [
      {
        key: "agents",
        label: "Agents",
        value: manifests.data ? `${list.length}` : "—",
        sub: manifests.data
          ? `${registered} registered${unregistered ? ` · ${unregistered} not registered` : ""}`
          : undefined,
      },
      {
        key: "calls",
        label: "Calls · 24h",
        value: vol(totals?.all),
        sub: "across all agents",
      },
      {
        key: "denied",
        label: "Denied · 24h",
        value: vol(totals?.deny),
        valueClass: totals && totals.deny > 0 ? "text-deny" : undefined,
        sub: summary.data
          ? `${denyRate ? `${denyRate}%` : "—"}${topDeny ? ` · mostly ${topDeny.key}` : ""}`
          : undefined,
      },
      {
        key: "approvals",
        // Sent for approval in the window, approved since or not.
        label: "Needs approval · 24h",
        value: vol(totals?.needs_approval),
        valueClass:
          totals && totals.needs_approval > 0 ? "text-approval" : undefined,
        sub: topApproval
          ? `${topApproval.key} ×${topApproval.needs_approval}`
          : undefined,
      },
    ];
  }, [manifests.data, summary.data]);

  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="agents" />;
  }

  // Old bookmarks of /agents?agent=X land on X's hub. Nothing in the app
  // links here any more, so this can go once those have aged out.
  const deepLink = searchParams.get("agent");
  if (deepLink) {
    return <Navigate replace to={agentHubPath(deepLink)} />;
  }

  const agents = manifests.data ?? [];
  // Filters set whose data isn't there: kept, but said so.
  const unapplied = [
    filters.activity.length > 0 && !summary.data && "24h activity",
    filters.delegation.length > 0 && !graphKnown && "delegation",
    filters.roles.length > 0 && !graphKnown && "roles",
  ].filter(Boolean) as string[];
  // Only while the toolbar is there: filters left from a bigger project
  // mustn't hide rows they can no longer clear.
  const toolbar = agents.length > AGENT_LIST_CAP;
  const q = toolbar ? query.trim().toLowerCase() : "";
  const f = toolbar ? filters : NO_FILTERS;
  const keep = new Set(facts.filter((a) => matches(f, a)).map((a) => a.name));
  const shown = agents.filter(
    (a) => keep.has(a.name) && (!q || a.name.toLowerCase().includes(q)),
  );

  return (
    <div className="mx-auto max-w-[1240px]">
      <PageHeader
        title="Agents"
        description={
          <>
            Every agent in this project, registered with{" "}
            <span className="font-mono text-foreground">hexgate register</span>{" "}
            or not yet. Open one for its graph, resources, policy and decisions.
          </>
        }
        actions={
          <DocsLink path={DOC_PATHS.registerAgent} label="Register an agent" />
        }
      />

      <div className="mb-4">
        <StatStrip stats={kpis} />
        {(summaryFailed || stale) && (
          <p className="mt-2 text-meta text-muted-foreground">
            {summaryFailed
              ? "Couldn't load the last 24 hours of activity."
              : "Couldn't refresh; this is what was last loaded."}
          </p>
        )}
        {graphFailed && (
          <p className="mt-2 text-meta text-muted-foreground">
            Delegation between agents isn't shown:{" "}
            {graph.error instanceof ApiError && graph.error.status === 422
              ? "the policy modules don't compose."
              : "couldn't load the policy graph."}
          </p>
        )}
      </div>

      {toolbar && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <SearchField
            value={query}
            onChange={setQuery}
            placeholder={`Filter ${agents.length} agents by name`}
            label="Filter agents by name"
            className="h-9 max-w-xs flex-1 basis-60 bg-card"
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-9 gap-2"
            onClick={() => setFiltering(true)}
          >
            Filters
            {activeCount(filters) > 0 && (
              <span className="grid h-5 min-w-5 place-items-center rounded-full bg-primary px-1.5 text-[11px] font-semibold text-primary-foreground">
                {activeCount(filters)}
              </span>
            )}
          </Button>
          <FilterChips f={filters} setF={setFilters} />
        </div>
      )}
      {toolbar && unapplied.length > 0 && (
        <p className="-mt-1 mb-3 text-meta text-muted-foreground">
          Not applied until their data loads: {unapplied.join(", ")}.
        </p>
      )}
      {filtering && (
        <AgentFilterDialog
          facts={facts}
          roles={graphKnown ? (graph.data?.roles ?? []) : []}
          activityKnown={!!summary.data}
          graphKnown={graphKnown}
          applied={filters}
          onApply={(next) => {
            setFilters(next);
            setFiltering(false);
          }}
          onClose={() => setFiltering(false)}
        />
      )}

      <Card className="overflow-hidden p-0">
        <nav aria-label="Agents" className="flex flex-col">
          {manifests.isPending ? (
            <div className="px-4 py-5 text-meta text-muted-foreground">
              Loading agents…
            </div>
          ) : manifests.isError && !manifests.data ? (
            <div className="px-4 py-5 text-meta text-muted-foreground">
              Couldn't load agents.
            </div>
          ) : agents.length ? (
            shown.length === 0 ? (
              <div className="px-4 py-5 text-meta text-muted-foreground">
                {q && activeCount(f) === 0
                  ? `No agent matches “${query.trim()}”.`
                  : "No agent matches these filters."}
              </div>
            ) : (
              shown.map((a, i) => {
                // From useId and the index: agent names may hold spaces.
                const id = `${rowId}-${i}`;
                const activity = activityOf(a.name);
                const registered = !!a.manifest;
                const tools = a.manifest?.tools?.length ?? 0;
                const skills = a.manifest?.skills?.length ?? 0;
                const model = a.manifest?.model?.trim();
                const reach = reachByAgent.get(a.name);
                const reachText = reach?.reaches.length
                  ? `reaches ${reach.reaches.join(", ")}`
                  : null;
                // Everything after the model, so a missing model never leaves a
                // dangling leading "· ". Outbound delegation ("reaches") rides
                // here; inbound ("called by") is a chip by the name instead.
                const facts = [
                  a.manifest?.framework?.trim(),
                  pluralise(tools, "tool"),
                  skills > 0 ? pluralise(skills, "skill") : null,
                  reachText,
                ]
                  .filter(Boolean)
                  .join(" · ");
                const calledBy = !!reach?.calledBy.length;
                return (
                  <Link
                    key={a.name}
                    to={agentHubPath(a.name)}
                    // The name alone; the rest of the row describes it.
                    aria-labelledby={`${id}-name`}
                    aria-describedby={[
                      `${id}-badge`,
                      calledBy && `${id}-chip`,
                      `${id}-facts`,
                      `${id}-counts`,
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    className="group flex items-center gap-3 border-t border-border/60 px-4 py-3.5 transition-colors first:border-t-0 hover:bg-accent/50"
                  >
                    <span
                      className={cn(
                        "size-1.5 shrink-0 rounded-full",
                        registered ? "bg-success" : "bg-muted-foreground/40",
                      )}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          id={`${id}-name`}
                          className="truncate font-mono text-[13px] font-medium"
                        >
                          {a.name}
                        </span>
                        <Badge
                          id={`${id}-badge`}
                          variant="outline"
                          className="font-mono text-[11px]"
                        >
                          {a.version !== null
                            ? `v${a.version}`
                            : "not registered"}
                        </Badge>
                        {calledBy && (
                          <span
                            id={`${id}-chip`}
                            className="rounded-full bg-primary/10 px-1.5 py-0.5 text-caption font-medium text-primary"
                          >
                            called by {reach!.calledBy.join(", ")}
                          </span>
                        )}
                      </div>
                      <div
                        id={`${id}-facts`}
                        className="mt-0.5 truncate text-meta text-muted-foreground"
                      >
                        {registered ? (
                          <>
                            {model && (
                              <span className="font-mono text-foreground">
                                {model}
                              </span>
                            )}
                            {model ? ` · ${facts}` : facts}
                          </>
                        ) : (
                          <>
                            not registered · run{" "}
                            <span className="font-mono">hexgate register</span>
                            {reachText && ` · ${reachText}`}
                          </>
                        )}
                      </div>
                    </div>
                    {/* Always there, "—" until the summary lands: an unregistered
                      agent can still be running and calling tools. */}
                    <div
                      id={`${id}-counts`}
                      className="hidden shrink-0 gap-5 text-right sm:flex"
                    >
                      <div>
                        <div className="text-[15px] font-semibold tabular-nums">
                          {activity ? activity.all.toLocaleString() : "—"}
                        </div>
                        <div className="text-caption text-muted-foreground">
                          calls 24h
                        </div>
                      </div>
                      <div>
                        <div
                          className={cn(
                            "text-[15px] font-semibold tabular-nums",
                            activity && activity.deny > 0
                              ? "text-deny"
                              : "text-muted-foreground",
                          )}
                        >
                          {activity ? activity.deny.toLocaleString() : "—"}
                        </div>
                        <div className="text-caption text-muted-foreground">
                          denied
                        </div>
                      </div>
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-foreground" />
                  </Link>
                );
              })
            )
          ) : (
            <div className="px-4 py-5 text-meta text-muted-foreground">
              No agents yet. Register one with{" "}
              <span className="font-mono text-foreground">
                hexgate register
              </span>
              .
            </div>
          )}
        </nav>
      </Card>
    </div>
  );
}

/** One removable chip per filter set, and Clear all. */
function FilterChips({
  f,
  setF,
}: {
  f: AgentFilters;
  setF: (f: AgentFilters) => void;
}) {
  const chips: { key: string; label: string; clear: () => void }[] = [
    ...(f.status === "any"
      ? []
      : [
          {
            key: "status",
            label: f.status === "registered" ? "Registered" : "Not registered",
            clear: () => setF({ ...f, status: "any" }),
          },
        ]),
    ...f.activity.map((v) => ({
      key: `a:${v}`,
      label: `${ACTIVITY_LABEL[v]} · 24h`,
      clear: () => setF({ ...f, activity: f.activity.filter((x) => x !== v) }),
    })),
    ...f.frameworks.map((v) => ({
      key: `f:${v}`,
      label: v,
      clear: () =>
        setF({ ...f, frameworks: f.frameworks.filter((x) => x !== v) }),
    })),
    ...f.delegation.map((v) => ({
      key: `d:${v}`,
      label: v === "reaches" ? "Reaches agents" : "Called by agents",
      clear: () =>
        setF({ ...f, delegation: f.delegation.filter((x) => x !== v) }),
    })),
    ...f.roles.map((v) => ({
      key: `r:${v}`,
      label: `Run by ${v}`,
      clear: () => setF({ ...f, roles: f.roles.filter((x) => x !== v) }),
    })),
  ];
  if (!chips.length) return null;
  return (
    <>
      {chips.map((c) => (
        <button
          key={c.key}
          type="button"
          onClick={c.clear}
          aria-label={`Remove filter: ${c.label}`}
          className="inline-flex h-8 items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-2.5 text-xs font-medium text-foreground hover:bg-primary/15"
        >
          {c.label}
          <X className="size-3" aria-hidden />
        </button>
      ))}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        onClick={() => setF(NO_FILTERS)}
      >
        Clear all
      </Button>
    </>
  );
}
