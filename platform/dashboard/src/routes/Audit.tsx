import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, type AuditOutcome } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { Card } from "@/components/ui/card";
import { DashboardRangePicker } from "@/components/ui/dashboard-range-picker";
import { type ChartBucket, StackedBars } from "@/components/ui/charts";
import { StatStrip } from "@/components/ui/stat-strip";
import { AuditOnboarding } from "@/components/audit/onboarding";
import { burstFilters } from "@/components/audit/anomalies";
import { type Counts } from "@/components/audit/charts";
import {
  displayNoValue,
  OUT_SWATCH,
  OUTCOME_SERIES,
  scopeNoValue,
} from "@/components/audit/chart-tokens";
import {
  EMPTY_AUDIT_FILTERS,
  fromAuditSearch,
  pageLimitFor,
  type SetAuditFilters,
  toAuditSearch,
  useAuditFilters,
  withAuditFilters,
} from "@/lib/audit-filters";
import { rangeDays, rangePhrase } from "@/lib/date-range";
import { DetailDrawer } from "@/components/audit/drawer";
import { useDecisionLog } from "@/lib/decision-log";
import {
  ActiveChips,
  AnomaliesCard,
  BreakdownCard,
  EventsTable,
  FilterBar,
} from "@/components/audit/pieces";
import { PageHeader } from "@/components/ui/page-header";

const ZERO_COUNTS: Counts = { allow: 0, deny: 0, needs_approval: 0, total: 0 };

/** "day", "hour", "6 hours", "15 minutes" for a bucket width. */
function bucketUnit(minutes: number): string {
  if (minutes >= 1440 && minutes % 1440 === 0)
    return minutes === 1440 ? "day" : `${minutes / 1440} days`;
  if (minutes % 60 === 0)
    return minutes === 60 ? "hour" : `${minutes / 60} hours`;
  return minutes === 1 ? "minute" : `${minutes} minutes`;
}

// ————————————————————————————————————————————— Root
export function AuditPage() {
  const projectScope = useProjectScoped();
  const projectId = projectScope.projectId;
  const navigate = useNavigate();
  const canManageBans = useCanManageBans();
  // Display name for the page subtitle; falls back to the id.
  const projectName = projectScope.projectName ?? projectId;
  // The URL is the source of truth for the filters, so a link, a reload or
  // a shared URL opens the same slice and the first render already queries
  // it. A bare /audit (the sidebar) is redirected to the last view.
  const [params, setParams] = useSearchParams();
  const lastSearch = useAuditFilters(
    (s) => (projectId && s.lastSearch[projectId]) || "",
  );
  const remember = useAuditFilters((s) => s.remember);
  const urlFilters = useMemo(() => fromAuditSearch(params), [params]);
  const saved = useMemo(
    () =>
      fromAuditSearch(new URLSearchParams(lastSearch)) ?? EMPTY_AUDIT_FILTERS,
    [lastSearch],
  );
  // The project this page's filters belong to. A switch while Audit is open
  // shows (and fetches) the new project's last view from the first render,
  // and the effect rewrites the URL to it; a link or reload only ever meets
  // the project it loads in, so it keeps its filters.
  const [viewProject, setViewProject] = useState(projectId);
  if (viewProject === null && projectId) setViewProject(projectId);
  const switching = !!projectId && !!viewProject && viewProject !== projectId;
  // The switch is done once the URL shows the new view.
  if (
    switching &&
    params.toString() === withAuditFilters(params, saved).toString()
  )
    setViewProject(projectId);
  const f = switching ? saved : (urlFilters ?? saved);
  const viewKey = toAuditSearch(f).toString();
  // Calls in one event compose: each builds on the previous one's result,
  // not on the render's `f`.
  const latest = useRef(f);
  useEffect(() => {
    latest.current = f;
  }, [f]);
  useEffect(() => {
    // Rewrite a bare /audit to the last view, and a malformed URL to what
    // the page actually shows, so a copied link matches the screen.
    // Wait for the project: the remembered view is per project.
    if (!projectId) return;
    if (params.toString() !== withAuditFilters(params, f).toString())
      setParams((prev) => withAuditFilters(prev, f), { replace: true });
    if (urlFilters && !switching) remember(projectId, viewKey);
  }, [
    switching,
    urlFilters,
    viewKey,
    f,
    params,
    projectId,
    remember,
    setParams,
  ]);
  const setF: SetAuditFilters = useCallback(
    (update) => {
      latest.current = update(latest.current);
      const next = latest.current;
      // A change made mid-switch builds on the new view and ends the switch.
      setViewProject(projectId);
      setParams((prev) => withAuditFilters(prev, next), { replace: true });
    },
    [setParams, projectId],
  );
  // Paging belongs to one view of one project; any other starts at one page.
  const pageKey = `${projectId}?${viewKey}`;
  const tableLimit = useAuditFilters((s) => pageLimitFor(s, pageKey));
  const setPageLimit = useAuditFilters((s) => s.setPageLimit);
  // UI state → wire: '' = "all" locally, so unset filters are omitted
  // (undefined). The "(none)" label (role/user) maps back to '' — the
  // wire's no-value bucket; no sentinel string leaves the dashboard.
  const scope = {
    window: f.range,
    agent: f.agent || undefined,
    role: scopeNoValue(f.role),
    tool: f.tool || undefined,
    start_date: f.start_date ? f.start_date.toISOString() : undefined,
    end_date: f.end_date ? f.end_date.toISOString() : undefined,
    user: scopeNoValue(f.user),
  };

  // Range-only (unscoped) summary: filter dropdown options + the "X of Y"
  // total. Shares the summaryQ key shape on purpose: React Query's key hash
  // drops undefined values, so when no filter is set this key hashes equal
  // to summaryQ's and both dedupe into ONE fetch + cache entry; with a
  // filter active the keys diverge and the second request is real.
  const optionsScope = {
    window: f.range,
    start_date: scope.start_date,
    end_date: scope.end_date,
  };
  const optionsQ = useQuery({
    queryKey: ["audit", "summary", projectId, optionsScope],
    enabled: !!projectId,
    queryFn: () => api.getAuditSummary(optionsScope, projectId as string),
  });
  // Scoped summary/timeseries: stat strip, chart, breakdown.
  const summaryQ = useQuery({
    queryKey: ["audit", "summary", projectId, scope],
    enabled: !!projectId,
    queryFn: () => api.getAuditSummary(scope, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  const tsQ = useQuery({
    queryKey: ["audit", "ts", projectId, scope],
    enabled: !!projectId,
    queryFn: () => api.getAuditTimeseries(scope, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  const listFilters = {
    ...scope,
    outcome: f.outcome || undefined,
    limit: tableLimit,
  };
  // The drawer's pick stays local and is dropped on a project switch.
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
    listFilters,
    (limit) => setPageLimit(pageKey, limit),
    "decisions.jsonl",
  );
  // Intentionally excludes user/agent/role/tool: the anomaly endpoint ranks
  // all users and must not be pre-filtered to a single one.
  const anomalyScope = {
    window: scope.window,
    start_date: scope.start_date,
    end_date: scope.end_date,
  };
  const anomaliesQ = useQuery({
    queryKey: ["audit", "anomalies", projectId, anomalyScope],
    enabled: !!projectId,
    queryFn: () => api.getAuditAnomalies(anomalyScope, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });

  const summary = summaryQ.data;
  const counts: Counts = summary
    ? {
        allow: summary.totals.allow,
        deny: summary.totals.deny,
        needs_approval: summary.totals.needs_approval,
        total: summary.totals.all,
      }
    : ZERO_COUNTS;

  // The server says how wide its buckets are (an older one doesn't: then
  // the gap between neighbours); null when there's nothing to tell by.
  const bucketMinutes = useMemo(() => {
    const d = tsQ.data;
    if (d?.[0]?.bucket_minutes) return d[0].bucket_minutes;
    if (!d || d.length < 2) return null;
    return Math.round((+new Date(d[1].bucket) - +new Date(d[0].bucket)) / 6e4);
  }, [tsQ.data]);
  const days: ChartBucket[] = useMemo(
    () =>
      (tsQ.data ?? []).map((p) => ({
        label: new Date(p.bucket),
        mode: (bucketMinutes ?? 0) >= 1440 ? "day" : "hour",
        values: {
          allow: p.allow,
          deny: p.deny,
          needs_approval: p.needs_approval,
        },
        total: p.allow + p.deny + p.needs_approval,
      })),
    [tsQ.data, bucketMinutes],
  );
  const spark = useMemo(
    () => ({
      total: days.map((d) => d.total),
      allow: days.map((d) => d.values.allow),
      deny: days.map((d) => d.values.deny),
      needs_approval: days.map((d) => d.values.needs_approval),
    }),
    [days],
  );

  const denyRate = counts.total
    ? ((counts.deny / counts.total) * 100).toFixed(1)
    : "0.0";
  const allowPct = counts.total
    ? Math.round((counts.allow / counts.total) * 100)
    : 0;
  const apprPct = counts.total
    ? Math.round((counts.needs_approval / counts.total) * 100)
    : 0;
  const nDays = rangeDays(f.range, f.start_date, f.end_date);
  const avgLabel =
    !f.start_date && f.range === "24h"
      ? `${(counts.total / 24).toFixed(1)}/hr avg`
      : `${(counts.total / nDays).toFixed(0)}/day avg`;
  const setOutcome = (o: AuditOutcome) =>
    setF((p) => ({ ...p, outcome: p.outcome === o ? "" : o }));

  const options = optionsQ.data;
  const rangeTotal = options?.totals.all ?? 0;
  // Filters that narrow the summary (outcome only narrows the log).
  const scopeFiltered = !!(f.agent || f.role || f.tool || f.user);
  const hasFilters = scopeFiltered || !!(f.outcome || f.start_date);
  // Nothing recorded in the range and nothing filtered out: show next steps
  // instead of a wall of zeros.
  const showOnboarding = optionsQ.isSuccess && rangeTotal === 0 && !hasFilters;
  const rangeText = rangePhrase(f.range, f.start_date);
  if (projectScope.status === "no-project") {
    return <NoProjectEmptyState resource="audit events" />;
  }

  return (
    <>
      <div className="mx-auto max-w-[1400px]">
        <PageHeader
          title="Audit"
          description={
            // Placeholder data is the previous range's: hold the numbers
            // until the new range lands.
            counts.total > 0 && !summaryQ.isPlaceholderData ? (
              <>
                <span className="font-semibold text-foreground">
                  {counts.total.toLocaleString()}
                </span>{" "}
                {scopeFiltered
                  ? `decisions match these filters ${rangeText}`
                  : `policy decisions ${rangeText} for `}
                {!scopeFiltered && (
                  <span className="font-mono text-foreground">
                    {projectName}
                  </span>
                )}
                . Deny rate{" "}
                <span className="font-semibold text-deny">{denyRate}%</span>.
              </>
            ) : (
              <>
                Every policy decision for project{" "}
                <span className="font-mono text-foreground">{projectName}</span>
              </>
            )
          }
          actions={
            <>
              <span
                className="flex items-center gap-1.5 text-meta text-muted-foreground"
                title="Refreshes every 30 seconds"
              >
                <span className="size-1.5 rounded-full bg-success" />
                Live
              </span>
              <DashboardRangePicker f={f} setF={setF} />
            </>
          }
        />

        <FilterBar
          f={f}
          setF={setF}
          shown={page?.total ?? 0}
          total={rangeTotal}
          agents={options?.by_agent.map((r) => r.key) ?? []}
          roles={options?.by_role.map((r) => displayNoValue(r).key) ?? []}
          tools={options?.by_tool.map((r) => r.key) ?? []}
        />
        <ActiveChips f={f} setF={setF} />

        {showOnboarding ? (
          <AuditOnboarding
            onWiden={
              f.range !== "90d"
                ? () =>
                    setF((p) => ({
                      ...p,
                      range: "90d",
                      customMode: false,
                      start_date: null,
                      end_date: null,
                    }))
                : undefined
            }
          />
        ) : (
          <div className="space-y-4">
            <StatStrip
              stats={[
                {
                  key: "total",
                  label: "Decisions",
                  value: counts.total.toLocaleString(),
                  sub: avgLabel,
                  spark: spark.total,
                  sparkColor: "hsl(var(--muted-foreground))",
                },
                {
                  key: "deny",
                  label: "Denied",
                  swatchClass: OUT_SWATCH.deny,
                  value: counts.deny.toLocaleString(),
                  valueClass: "text-deny",
                  sub: `${denyRate}% deny rate`,
                  spark: spark.deny,
                  sparkColor: "hsl(var(--semantic-deny))",
                  onClick: () => setOutcome("deny"),
                  active: f.outcome === "deny",
                },
                {
                  key: "needs_approval",
                  label: "Needs approval",
                  swatchClass: OUT_SWATCH.needs_approval,
                  value: counts.needs_approval.toLocaleString(),
                  valueClass: "text-approval",
                  sub: `${apprPct}% of decisions`,
                  spark: spark.needs_approval,
                  sparkColor: "hsl(var(--semantic-approval))",
                  onClick: () => setOutcome("needs_approval"),
                  active: f.outcome === "needs_approval",
                },
                {
                  key: "allow",
                  label: "Allowed",
                  swatchClass: OUT_SWATCH.allow,
                  value: counts.allow.toLocaleString(),
                  sub: `${allowPct}% of decisions`,
                  spark: spark.allow,
                  sparkColor: "hsl(var(--semantic-allow))",
                  onClick: () => setOutcome("allow"),
                  active: f.outcome === "allow",
                },
              ]}
              split={(["deny", "needs_approval", "allow"] as const).map(
                (key) => ({
                  key,
                  value: counts[key],
                  className: OUT_SWATCH[key],
                }),
              )}
            />

            {!!anomaliesQ.data?.length && (
              <AnomaliesCard
                rows={anomaliesQ.data}
                onRowClick={(from, to, userId) =>
                  setF((p) => ({ ...p, ...burstFilters(userId, from, to) }))
                }
                onBanUser={
                  canManageBans
                    ? (userId) =>
                        navigate(`/bans?ban_user=${encodeURIComponent(userId)}`)
                    : undefined
                }
              />
            )}

            <div className="grid gap-4 lg:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
              <Card className="p-5">
                <div className="mb-5 flex flex-wrap items-baseline justify-between gap-2">
                  <h2 className="text-body font-semibold">
                    {bucketMinutes === null
                      ? "Decisions over time"
                      : `Decisions per ${bucketUnit(bucketMinutes)}`}
                    {/* Server day buckets are UTC days; say so, since the
                        log below groups by local day. */}
                    {bucketMinutes !== null && bucketMinutes >= 1440 && (
                      <span className="ml-1.5 text-meta font-normal text-muted-foreground">
                        UTC
                      </span>
                    )}
                  </h2>
                  <div className="flex gap-3.5 text-xs text-muted-foreground">
                    {[...OUTCOME_SERIES].reverse().map((s) => (
                      <span key={s.key} className="flex items-center gap-1.5">
                        <span
                          className={`size-2 rounded-sm ${s.swatchClass}`}
                        />
                        {s.label}
                      </span>
                    ))}
                  </div>
                </div>
                {days.some((d) => d.total > 0) ? (
                  <StackedBars
                    buckets={days}
                    series={OUTCOME_SERIES}
                    bucketMinutes={bucketMinutes ?? undefined}
                  />
                ) : (
                  <div className="flex h-52 items-center justify-center text-meta text-muted-foreground">
                    No decisions in this range.
                  </div>
                )}
              </Card>
              <BreakdownCard
                byTool={summary?.by_tool ?? []}
                byAgent={summary?.by_agent ?? []}
                byRole={summary?.by_role.map(displayNoValue) ?? []}
                f={f}
                setF={setF}
              />
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
                  : listQ.isError && !page
                    ? "Couldn't load decisions. Try refreshing the page."
                    : undefined
              }
            />
          </div>
        )}
      </div>

      <DetailDrawer
        event={sel}
        related={related}
        projectId={projectId}
        onClose={clearSel}
        onSelect={setSel}
        setF={setF}
      />
    </>
  );
}
