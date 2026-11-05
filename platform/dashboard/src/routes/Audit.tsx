import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, type AuditOutcome } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { DashboardRangePicker } from "@/components/ui/dashboard-range-picker";
import { AuditOnboarding } from "@/components/audit/onboarding";
import { burstFilters } from "@/components/audit/anomalies";
import { displayNoValue, scopeNoValue } from "@/components/audit/chart-tokens";
import {
  EMPTY_AUDIT_FILTERS,
  fromAuditSearch,
  pageLimitFor,
  type SetAuditFilters,
  toAuditSearch,
  useAuditFilters,
  withAuditFilters,
} from "@/lib/audit-filters";
import { rangePhrase } from "@/lib/date-range";
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
import { DecisionsChartCard, OutcomeStatStrip } from "@/components/audit/stats";
import {
  useAuditStatsQueries,
  useOutcomeStats,
} from "@/components/audit/outcome-stats";

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
  const { summaryQ, tsQ } = useAuditStatsQueries(scope, projectId);
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
  const stats = useOutcomeStats(
    summary,
    tsQ.data,
    f.range,
    f.start_date,
    f.end_date,
  );
  const { counts, denyRate } = stats;
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
            <OutcomeStatStrip
              stats={stats}
              onOutcome={setOutcome}
              activeOutcome={f.outcome}
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
              <DecisionsChartCard
                stats={stats}
                status={tsQ.isError && !tsQ.data ? "error" : undefined}
              />
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
