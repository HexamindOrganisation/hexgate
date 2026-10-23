import { useMemo, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
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
import { burstFilters, rankAnomalies } from "@/components/audit/anomalies";
import { OUTCOME_SERIES } from "@/components/audit/chart-tokens";
import { argsPreview } from "@/components/audit/fmt";
import { OutcomeLabel } from "@/components/audit/pieces";
import { formatRelative } from "@/components/bans/format";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { type ChartBucket, StackedBars } from "@/components/ui/charts";
import { PageHeader } from "@/components/ui/page-header";
import { useProjectScoped } from "@/lib/active";
import {
  api,
  type AgentManifestView,
  type AuditAnomaly,
  type AuditSummary,
  type TokenListItem,
} from "@/lib/api";
import { auditUrl } from "@/lib/audit-filters";
import { useAuditSummary24h } from "@/lib/audit-summary";
import { fmtTokens, pluralise } from "@/lib/format";
import { isStaleKey, STALE_KEY_DAYS } from "@/lib/keys";
import { cn } from "@/lib/utils";

const LIVE_MS = 15_000;
const SLOW_MS = 60_000;
const ATTENTION_BURSTS = 2;

function greeting(now = new Date()): string {
  const h = now.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

/** A titled card with an optional link in its header. */
function Panel({
  title,
  aside,
  children,
}: {
  title: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card className="overflow-hidden p-0">
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

/** The state line a panel shows instead of data: loading, failed, or empty.
 * Never reads as "all clear" before the data has actually arrived. */
function PanelNote({ children }: { children: ReactNode }) {
  return (
    <div className="px-5 py-6 text-meta text-muted-foreground">{children}</div>
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

function deriveAttention({
  anomalies,
  totals,
  agents,
  tokens,
  keysFetchedAt,
}: {
  anomalies: AuditAnomaly[];
  totals: AuditSummary["totals"] | undefined;
  agents: AgentManifestView[];
  tokens: TokenListItem[];
  keysFetchedAt: number;
}): Attention[] {
  const out: Attention[] = [];
  const ranked = rankAnomalies(anomalies);
  for (const a of ranked.slice(0, ATTENTION_BURSTS)) {
    out.push({
      key: `anomaly-${a.user_id}-${a.first_seen}`,
      icon: <TriangleAlert />,
      tone: "bg-deny/15 text-deny",
      title: (
        <>
          Denial burst from{" "}
          <span className="font-mono">{a.user_id || "(no user id)"}</span>
        </>
      ),
      sub: `${a.deny} of ${a.all} calls denied · ${formatRelative(a.last_seen)}`,
      cta: "Inspect",
      to: auditUrl(
        burstFilters(a.user_id, new Date(a.first_seen), new Date(a.last_seen)),
      ),
    });
  }
  if (ranked.length > ATTENTION_BURSTS) {
    const more = ranked.length - ATTENTION_BURSTS;
    out.push({
      key: "anomaly-more",
      icon: <TriangleAlert />,
      tone: "bg-deny/15 text-deny",
      title: `${more} more denial ${more === 1 ? "burst" : "bursts"} this week`,
      sub: "Ranked by severity and deny rate on Audit",
      cta: "Open audit",
      to: auditUrl({ range: "7d" }),
    });
  }
  if (totals?.needs_approval) {
    const n = totals.needs_approval;
    out.push({
      key: "approvals",
      icon: <Hourglass />,
      tone: "bg-approval/15 text-approval",
      title: `${pluralise(n, "call")} sent for approval in the last 24 hours`,
      sub: "Gated by policy rather than allowed or denied outright",
      cta: "Review",
      to: auditUrl({ range: "24h", outcome: "needs_approval" }),
    });
  }
  const unregistered = agents.filter((a) => !a.manifest);
  if (unregistered.length) {
    out.push({
      key: "unregistered",
      icon: <Bot />,
      tone: "bg-primary/15 text-primary",
      title: `${unregistered.length} ${unregistered.length === 1 ? "agent has" : "agents have"} no registered manifest`,
      sub: unregistered.map((a) => a.name).join(", "),
      cta: "Agents",
      to: "/agents",
    });
  }
  const stale = tokens.filter((t) => isStaleKey(t, keysFetchedAt));
  if (stale.length) {
    out.push({
      key: "keys",
      icon: <KeyRound />,
      tone: "bg-muted text-foreground",
      title: `${stale.length} API ${stale.length === 1 ? "key" : "keys"} unused for ${STALE_KEY_DAYS}+ days`,
      sub: stale.map((t) => t.name).join(", "),
      cta: "Review keys",
      to: "/tokens",
    });
  }
  return out;
}

export function HomePage() {
  const scope = useProjectScoped();
  const pid = scope.projectId;
  const on = !!pid;

  const summaryQ = useAuditSummary24h(pid, { poll: true });
  const weekQ = useQuery({
    queryKey: ["audit", "ts", pid, { window: "7d" }],
    enabled: on,
    queryFn: () => api.getAuditTimeseries({ window: "7d" }, pid!),
    refetchInterval: SLOW_MS,
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
    refetchInterval: SLOW_MS,
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
    refetchInterval: SLOW_MS,
  });

  const totals = summaryQ.data?.totals;
  const denyRate =
    totals && totals.all ? ((totals.deny / totals.all) * 100).toFixed(1) : null;
  const agents = useMemo(() => agentsQ.data ?? [], [agentsQ.data]);

  const attention = useMemo(
    () =>
      deriveAttention({
        anomalies: anomaliesQ.data ?? [],
        totals,
        agents,
        tokens: tokensQ.data ?? [],
        keysFetchedAt: tokensQ.dataUpdatedAt,
      }),
    [anomaliesQ.data, totals, agents, tokensQ.data, tokensQ.dataUpdatedAt],
  );
  // Each burst counts once; the "N more" row stands for N of them.
  const attentionCount =
    attention.length -
    (attention.some((t) => t.key === "anomaly-more") ? 1 : 0) +
    Math.max((anomaliesQ.data?.length ?? 0) - ATTENTION_BURSTS, 0);
  // Attention needs the signals that can raise an alarm; until they land
  // (or if they fail) it must not claim the coast is clear.
  const attentionPending =
    anomaliesQ.isPending || summaryQ.isPending || agentsQ.isPending;
  const attentionFailed =
    anomaliesQ.isError || summaryQ.isError || agentsQ.isError;

  // 7d comes back in 6-hour buckets; roll them up to UTC days (the window
  // touches 8 of them, the first and last partly).
  const week = useMemo(() => {
    const byDay = new Map<string, ChartBucket>();
    for (const p of weekQ.data ?? []) {
      const day = new Date(p.bucket).toISOString().slice(0, 10);
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
    const buckets = [...byDay.values()];
    const sum = (k: "deny" | "needs_approval") =>
      buckets.reduce((s, b) => s + b.values[k], 0);
    return {
      buckets,
      all: buckets.reduce((s, b) => s + b.total, 0),
      deny: sum("deny"),
      appr: sum("needs_approval"),
    };
  }, [weekQ.data]);

  const byAgent = useMemo(
    () => new Map(summaryQ.data?.by_agent.map((r) => [r.key, r])),
    [summaryQ.data],
  );
  const maxAgentCalls = Math.max(
    ...agents.map((a) => byAgent.get(a.name)?.all ?? 0),
    1,
  );
  const lastDecision = feedQ.data?.rows[0]?.occurred_at;

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
              {agentsQ.isSuccess && (
                <>{pluralise(agents.length, "agent")} in </>
              )}
              <span className="font-mono text-foreground">
                {scope.projectName}
              </span>
              {lastDecision && (
                <> · last decision {formatRelative(lastDecision)}</>
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
              <Link to={auditUrl()}>
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
              {attentionCount > 0 && (
                <span className="font-medium text-muted-foreground">
                  · {attentionCount}
                </span>
              )}
            </>
          }
        >
          {attention.map((t) => (
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
          ))}
          {attentionFailed ? (
            <PanelNote>
              Couldn't load every signal, so this list may be incomplete.
            </PanelNote>
          ) : attentionPending ? (
            <PanelNote>Checking…</PanelNote>
          ) : (
            !attention.length && (
              <div className="flex items-center gap-3 px-5 py-6 text-meta text-muted-foreground">
                <CircleCheck className="size-5 text-success" />
                Nothing needs a look right now.
              </div>
            )
          )}
        </Panel>

        <Panel
          title="Last 24 hours"
          aside={<PanelLink to="/usage">Usage</PanelLink>}
        >
          <div className="grid grid-cols-2 [&>*:nth-child(-n+2)]:border-b [&>*:nth-child(even)]:border-l [&>*]:border-border/60">
            {[
              {
                label: "Decisions",
                value: totals?.all.toLocaleString(),
                sub: summaryQ.isError ? "couldn't load" : "all outcomes",
              },
              {
                label: "Denied",
                value: totals?.deny.toLocaleString(),
                sub: denyRate ? `${denyRate}% deny rate` : "—",
                cls: "text-deny",
              },
              {
                label: "Needs approval",
                value: totals?.needs_approval.toLocaleString(),
                sub: "sent for approval",
                cls: "text-approval",
              },
              {
                label: "Tokens",
                value:
                  usageQ.data && fmtTokens(usageQ.data.totals.total_tokens),
                sub: usageQ.data
                  ? `across ${usageQ.data.totals.calls.toLocaleString()} model calls`
                  : usageQ.isError
                    ? "usage unavailable"
                    : "—",
              },
            ].map((s) => (
              <div key={s.label} className="flex flex-col gap-1.5 px-5 py-4">
                <span className="text-meta font-medium text-muted-foreground">
                  {s.label}
                </span>
                <span
                  className={cn(
                    "text-[28px] font-semibold leading-none tracking-tight tabular-nums",
                    s.value !== undefined && s.cls,
                  )}
                >
                  {s.value ?? "—"}
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
          aside={
            <PanelLink to={auditUrl({ range: "24h" })}>View all</PanelLink>
          }
        >
          {feedQ.isPending ? (
            <PanelNote>Loading…</PanelNote>
          ) : feedQ.isError ? (
            <PanelNote>Couldn't load recent decisions.</PanelNote>
          ) : feedQ.data.rows.length ? (
            feedQ.data.rows.map((r) => (
              <Link
                key={r.event_id}
                to={auditUrl({ range: "24h" })}
                className="grid grid-cols-[64px_84px_minmax(0,1fr)_110px] items-center gap-3 border-t border-border/60 px-5 py-2.5 text-meta first:border-t-0 hover:bg-accent/50"
              >
                <span className="font-mono text-xs text-muted-foreground">
                  {formatRelative(r.occurred_at)}
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
            <PanelNote>
              No decisions in the last 24 hours.{" "}
              <Link to={auditUrl()} className="text-primary hover:underline">
                Getting started
              </Link>
            </PanelNote>
          )}
        </Panel>

        <Panel
          title="Agents"
          aside={<PanelLink to="/agents">Manage</PanelLink>}
        >
          {agentsQ.isPending ? (
            <PanelNote>Loading…</PanelNote>
          ) : agentsQ.isError ? (
            <PanelNote>Couldn't load agents.</PanelNote>
          ) : agents.length ? (
            agents.map((a) => {
              const row = byAgent.get(a.name);
              return (
                <Link
                  key={a.name}
                  to={`/agents?agent=${encodeURIComponent(a.name)}`}
                  className="grid grid-cols-[minmax(0,1fr)_170px] items-center gap-3 border-t border-border/60 px-5 py-3 first:border-t-0 hover:bg-accent/50"
                >
                  <span className="min-w-0">
                    <span className="block truncate font-mono text-[13px] font-medium">
                      {a.name}
                    </span>
                    <span className="block text-meta text-muted-foreground">
                      {a.manifest
                        ? pluralise(a.manifest.tools?.length ?? 0, "tool")
                        : "not registered"}
                    </span>
                  </span>
                  <span className="flex flex-col items-end gap-1.5">
                    <span className="whitespace-nowrap text-meta text-muted-foreground">
                      <span className="font-semibold text-foreground">
                        {row?.all ?? 0}
                      </span>{" "}
                      in 24h
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
            <PanelNote>
              No agents yet. Register one with{" "}
              <span className="font-mono">hexgate register</span>.
            </PanelNote>
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
          weekQ.isSuccess && (
            <span className="text-meta text-muted-foreground">
              {week.all.toLocaleString()} decisions ·{" "}
              {week.deny.toLocaleString()} denied · {week.appr.toLocaleString()}{" "}
              sent for approval
            </span>
          )
        }
      >
        <div className="p-5">
          {weekQ.isPending ? (
            <div className="h-28 text-meta text-muted-foreground">Loading…</div>
          ) : weekQ.isError ? (
            <div className="h-28 text-meta text-muted-foreground">
              Couldn't load the week.
            </div>
          ) : week.all > 0 ? (
            <StackedBars
              buckets={week.buckets}
              series={OUTCOME_SERIES}
              height={120}
            />
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
