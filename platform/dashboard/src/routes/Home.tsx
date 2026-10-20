import { useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { formatDistanceToNowStrict } from "date-fns";
import {
  ArrowRight,
  Bot,
  CircleCheck,
  Hourglass,
  KeyRound,
  ScrollText,
  TriangleAlert,
} from "lucide-react";
import { Link } from "react-router-dom";

import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { OUTCOME_SERIES } from "@/components/audit/chart-tokens";
import { argsPreview, OutcomeLabel } from "@/components/audit/pieces";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { type ChartBucket, StackedBars } from "@/components/ui/charts";
import { PageHeader } from "@/components/ui/page-header";
import { useProjectScoped } from "@/lib/active";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

const LIVE_MS = 15_000;
const STALE_KEY_DAYS = 30;

function greeting(now = new Date()): string {
  const h = now.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

const compact = (n: number) =>
  Intl.NumberFormat("en-US", { notation: "compact" }).format(n);

/** A titled card with an optional link in its header. */
function Panel({
  title,
  aside,
  children,
  className,
}: {
  title: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("overflow-hidden p-0", className)}>
      <div className="flex items-center gap-3 border-b border-border px-5 py-3.5">
        <h2 className="text-body font-semibold">{title}</h2>
        {aside && <div className="ml-auto">{aside}</div>}
      </div>
      {children}
    </Card>
  );
}

function PanelLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="text-meta font-medium text-primary hover:underline"
    >
      {children}
    </Link>
  );
}

interface Attention {
  key: string;
  icon: ReactNode;
  tone: string;
  title: ReactNode;
  sub: ReactNode;
  cta: string;
  to: string;
}

export function HomePage() {
  const scope = useProjectScoped();
  const pid = scope.projectId;
  const on = !!pid;

  const summaryQ = useQuery({
    queryKey: ["audit", "summary", pid, { window: "24h" }],
    enabled: on,
    queryFn: () => api.getAuditSummary({ window: "24h" }, pid!),
    refetchInterval: 30_000,
  });
  const weekQ = useQuery({
    queryKey: ["audit", "ts", pid, { window: "7d" }],
    enabled: on,
    queryFn: () => api.getAuditTimeseries({ window: "7d" }, pid!),
    refetchInterval: 60_000,
  });
  const feedQ = useQuery({
    queryKey: ["audit", "list", pid, { window: "24h", limit: 7 }],
    enabled: on,
    queryFn: () => api.listAuditDecisions({ window: "24h", limit: 7 }, pid!),
    refetchInterval: LIVE_MS,
  });
  const anomaliesQ = useQuery({
    queryKey: ["audit", "anomalies", pid, { window: "7d" }],
    enabled: on,
    queryFn: () => api.getAuditAnomalies({ window: "7d" }, pid!),
  });
  const agentsQ = useQuery({
    queryKey: ["agent-manifests", pid],
    enabled: on,
    queryFn: () => api.listAgentManifests(pid!),
  });
  // Keys and usage are best-effort: a member without access just doesn't
  // get those rows.
  const tokensQ = useQuery({
    queryKey: ["tokens", pid],
    enabled: on,
    queryFn: () => api.listTokens(pid!),
    retry: false,
  });
  const usageQ = useQuery({
    queryKey: ["usage", "summary", pid, { window: "24h" }],
    enabled: on,
    queryFn: () => api.getLlmUsageSummary({ window: "24h" }, pid!),
    retry: false,
  });

  const totals = summaryQ.data?.totals;
  const denyRate =
    totals && totals.all ? ((totals.deny / totals.all) * 100).toFixed(1) : null;

  // 7d comes back in 6-hour buckets; roll them up to UTC days.
  const week: ChartBucket[] = useMemo(() => {
    const byDay = new Map<string, ChartBucket>();
    for (const p of weekQ.data ?? []) {
      const day = p.bucket.slice(0, 10);
      const b = byDay.get(day) ?? {
        label: new Date(`${day}T00:00:00Z`),
        mode: "day" as const,
        values: { allow: 0, deny: 0, needs_approval: 0 },
        total: 0,
      };
      b.values.allow += p.allow;
      b.values.deny += p.deny;
      b.values.needs_approval += p.needs_approval;
      b.total += p.allow + p.deny + p.needs_approval;
      byDay.set(day, b);
    }
    return [...byDay.values()].slice(-7);
  }, [weekQ.data]);
  const weekTotals = week.reduce(
    (s, b) => ({
      all: s.all + b.total,
      deny: s.deny + b.values.deny,
      appr: s.appr + b.values.needs_approval,
    }),
    { all: 0, deny: 0, appr: 0 },
  );

  const agents = agentsQ.data ?? [];
  const byAgent = new Map(summaryQ.data?.by_agent.map((r) => [r.key, r]));
  const maxAgentCalls = Math.max(
    ...agents.map((a) => byAgent.get(a.name)?.all ?? 0),
    1,
  );
  const lastDecision = feedQ.data?.rows[0]?.occurred_at;

  const attention: Attention[] = [];
  for (const a of [...(anomaliesQ.data ?? [])]
    .sort(
      (x, y) => Number(y.severity === "high") - Number(x.severity === "high"),
    )
    .slice(0, 2)) {
    attention.push({
      key: `anomaly-${a.user_id}-${a.first_seen}`,
      icon: <TriangleAlert />,
      tone: "bg-deny/15 text-deny",
      title: (
        <>
          Denial burst from <span className="font-mono">{a.user_id}</span>
        </>
      ),
      sub: `${a.deny} of ${a.all} calls denied · ${formatDistanceToNowStrict(new Date(a.last_seen))} ago`,
      cta: "Inspect",
      to: "/audit",
    });
  }
  if (totals?.needs_approval) {
    attention.push({
      key: "approvals",
      icon: <Hourglass />,
      tone: "bg-approval/15 text-approval",
      title: `${totals.needs_approval} ${totals.needs_approval === 1 ? "call" : "calls"} sent for approval today`,
      sub: "Gated by policy rather than allowed or denied outright",
      cta: "Review",
      to: "/audit",
    });
  }
  const unregistered = agents.filter((a) => !a.manifest);
  if (unregistered.length) {
    attention.push({
      key: "unregistered",
      icon: <Bot />,
      tone: "bg-primary/15 text-primary",
      title: `${unregistered.length} ${unregistered.length === 1 ? "agent has" : "agents have"} no registered manifest`,
      sub: unregistered.map((a) => a.name).join(", "),
      cta: "Agents",
      to: "/agents",
    });
  }
  const staleKeys = (tokensQ.data ?? []).filter(
    (t) =>
      !t.last_used_at ||
      Date.now() - +new Date(t.last_used_at) > STALE_KEY_DAYS * 864e5,
  );
  if (staleKeys.length) {
    attention.push({
      key: "keys",
      icon: <KeyRound />,
      tone: "bg-muted text-foreground",
      title: `${staleKeys.length} API ${staleKeys.length === 1 ? "key" : "keys"} unused for ${STALE_KEY_DAYS}+ days`,
      sub: staleKeys.map((t) => t.name).join(", "),
      cta: "Review keys",
      to: "/tokens",
    });
  }

  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="project overview" />;
  }

  return (
    <div className="mx-auto max-w-[1240px] space-y-4">
      <PageHeader
        className="mb-2"
        title={greeting()}
        description={
          scope.projectName && (
            <>
              {agents.length} {agents.length === 1 ? "agent" : "agents"} in{" "}
              <span className="font-mono text-foreground">
                {scope.projectName}
              </span>
              {lastDecision && (
                <>
                  {" "}
                  · last decision{" "}
                  {formatDistanceToNowStrict(new Date(lastDecision))} ago
                </>
              )}
            </>
          )
        }
        actions={
          <>
            <Button asChild variant="outline">
              <Link to="/policies">Edit policies</Link>
            </Button>
            <Button asChild className="gap-1.5">
              <Link to="/audit">
                <ScrollText className="size-4" />
                Open audit
              </Link>
            </Button>
          </>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
        <Panel
          title={
            <>
              Needs attention{" "}
              {attention.length > 0 && (
                <span className="font-medium text-muted-foreground">
                  · {attention.length}
                </span>
              )}
            </>
          }
        >
          {attention.length ? (
            attention.map((t) => (
              <Link
                key={t.key}
                to={t.to}
                className="grid grid-cols-[32px_minmax(0,1fr)_auto] items-center gap-3.5 border-t border-border/60 px-5 py-3.5 first:border-t-0 hover:bg-accent/50"
              >
                <span
                  className={cn(
                    "grid size-8 place-items-center rounded-lg [&_svg]:size-4",
                    t.tone,
                  )}
                >
                  {t.icon}
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold">{t.title}</span>
                  <span className="block truncate text-meta text-muted-foreground">
                    {t.sub}
                  </span>
                </span>
                <span className="flex items-center gap-1 text-meta font-medium text-primary">
                  {t.cta}
                  <ArrowRight className="size-3.5" />
                </span>
              </Link>
            ))
          ) : (
            <div className="flex items-center gap-3 px-5 py-6 text-meta text-muted-foreground">
              <CircleCheck className="size-5 text-success" />
              Nothing needs a look right now.
            </div>
          )}
        </Panel>

        <Panel title="Today" aside={<PanelLink to="/usage">Usage</PanelLink>}>
          <div className="grid grid-cols-2 [&>*:nth-child(-n+2)]:border-b [&>*:nth-child(even)]:border-l [&>*]:border-border/60">
            {[
              {
                label: "Decisions",
                value: totals ? totals.all.toLocaleString() : "—",
                sub: "last 24 hours",
              },
              {
                label: "Denied",
                value: totals ? totals.deny.toLocaleString() : "—",
                sub: denyRate ? `${denyRate}% deny rate` : "—",
                cls: "text-deny",
              },
              {
                label: "Needs approval",
                value: totals ? totals.needs_approval.toLocaleString() : "—",
                sub: "sent for approval",
                cls: "text-approval",
              },
              {
                label: "Tokens",
                value: usageQ.data
                  ? compact(usageQ.data.totals.total_tokens)
                  : "—",
                sub: usageQ.data
                  ? `across ${usageQ.data.totals.calls.toLocaleString()} model calls`
                  : "usage unavailable",
              },
            ].map((s) => (
              <div key={s.label} className="flex flex-col gap-1.5 px-5 py-4">
                <span className="text-meta font-medium text-muted-foreground">
                  {s.label}
                </span>
                <span
                  className={cn(
                    "text-[28px] font-semibold leading-none tracking-tight tabular-nums",
                    s.cls,
                  )}
                >
                  {s.value}
                </span>
                <span className="text-meta text-muted-foreground">{s.sub}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
        <Panel
          title={
            <span className="flex items-center gap-2">
              Live decisions
              <span className="flex items-center gap-1.5 text-xs font-normal text-muted-foreground">
                <span className="size-1.5 rounded-full bg-success" />
                every {LIVE_MS / 1000}s
              </span>
            </span>
          }
          aside={<PanelLink to="/audit">View all</PanelLink>}
        >
          {feedQ.data?.rows.length ? (
            feedQ.data.rows.map((r) => (
              <Link
                key={r.event_id}
                to="/audit"
                className="grid grid-cols-[56px_84px_minmax(0,1fr)_110px] items-center gap-3 border-t border-border/60 px-5 py-2.5 text-meta first:border-t-0 hover:bg-accent/50"
              >
                <span className="font-mono text-xs text-muted-foreground">
                  {formatDistanceToNowStrict(new Date(r.occurred_at), {
                    roundingMethod: "floor",
                  })
                    .replace(/ seconds?/, "s")
                    .replace(/ minutes?/, "m")
                    .replace(/ hours?/, "h")}
                </span>
                <OutcomeLabel d={r.outcome} />
                <span className="truncate font-mono">
                  {r.tool_name}
                  <span className="text-muted-foreground">
                    {argsPreview(r.arguments)}
                  </span>
                </span>
                <span className="truncate text-right font-mono text-xs text-muted-foreground">
                  {r.agent_name}
                </span>
              </Link>
            ))
          ) : (
            <div className="px-5 py-6 text-meta text-muted-foreground">
              No decisions in the last 24 hours.{" "}
              <Link to="/audit" className="text-primary hover:underline">
                Getting started
              </Link>
            </div>
          )}
        </Panel>

        <Panel
          title="Agents"
          aside={<PanelLink to="/agents">Manage</PanelLink>}
        >
          {agents.length ? (
            agents.map((a) => {
              const row = byAgent.get(a.name);
              return (
                <Link
                  key={a.name}
                  to="/agents"
                  className="grid grid-cols-[minmax(0,1fr)_170px] items-center gap-3 border-t border-border/60 px-5 py-3 first:border-t-0 hover:bg-accent/50"
                >
                  <span className="min-w-0">
                    <span className="block truncate font-mono text-[13px] font-medium">
                      {a.name}
                    </span>
                    <span className="block text-meta text-muted-foreground">
                      {a.manifest
                        ? `${a.manifest.tools?.length ?? 0} tools`
                        : "not registered"}
                    </span>
                  </span>
                  <span className="flex flex-col items-end gap-1.5">
                    <span className="whitespace-nowrap text-meta text-muted-foreground">
                      <span className="font-semibold text-foreground">
                        {row?.all ?? 0}
                      </span>{" "}
                      today
                      {!!row?.deny && (
                        <span className="text-deny"> · {row.deny} denied</span>
                      )}
                    </span>
                    <span className="block h-1 w-full rounded-full bg-muted">
                      <span
                        className="block h-1 rounded-full bg-allow-fill"
                        style={{
                          width: `${((row?.all ?? 0) / maxAgentCalls) * 100}%`,
                        }}
                      />
                    </span>
                  </span>
                </Link>
              );
            })
          ) : (
            <div className="px-5 py-6 text-meta text-muted-foreground">
              No agents yet. Register one with{" "}
              <span className="font-mono">hexgate register</span>.
            </div>
          )}
        </Panel>
      </div>

      <Panel
        title={
          <>
            Last 7 days{" "}
            <span className="ml-1 text-meta font-normal text-muted-foreground">
              UTC
            </span>
          </>
        }
        aside={
          <span className="text-meta text-muted-foreground">
            {weekTotals.all.toLocaleString()} decisions ·{" "}
            {weekTotals.deny.toLocaleString()} denied ·{" "}
            {weekTotals.appr.toLocaleString()} sent for approval
          </span>
        }
      >
        <div className="p-5">
          {weekTotals.all > 0 ? (
            <StackedBars buckets={week} series={OUTCOME_SERIES} height={120} />
          ) : (
            <div className="flex h-28 items-center justify-center text-meta text-muted-foreground">
              No decisions this week.
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}
