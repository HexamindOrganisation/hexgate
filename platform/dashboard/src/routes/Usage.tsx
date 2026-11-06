import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { DashboardRangePicker } from "@/components/ui/dashboard-range-picker";
import { displayNoValue, scopeNoValue } from "@/components/audit/chart-tokens";
import { rangePhrase } from "@/lib/date-range";
import {
  UsageActiveChips,
  UsageBreakdownCard,
  UsageFilterBar,
  UsageStatStrip,
} from "@/components/usage/pieces";
import { TopUsersCard } from "@/components/usage/top-users";
import { fromUsageSearch, useUsageFilters } from "@/lib/usage-filters";
import { PageHeader } from "@/components/ui/page-header";
import { fmtTokens, pluralise } from "@/lib/format";

export function UsagePage() {
  const projectScope = useProjectScoped();
  const projectId = projectScope.projectId;
  const projectName = projectScope.projectName ?? projectId;

  const f = useUsageFilters((s) => s.filters);
  const setF = useUsageFilters((s) => s.setFilters);
  // A link with filters (e.g. the agent hub's drill-down) seeds the store
  // once; the query is then dropped so the store stays the source of truth.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    const linked = fromUsageSearch(params);
    if (!linked) return;
    setF(() => linked);
    setParams({}, { replace: true });
  }, [params, setF, setParams]);

  const scope = {
    window: f.range,
    agent: f.agent || undefined,
    model: f.model || undefined,
    user: scopeNoValue(f.user),
    start_date: f.start_date ? f.start_date.toISOString() : undefined,
    end_date: f.end_date ? f.end_date.toISOString() : undefined,
  };
  // Range-only (unscoped) summary: filter dropdown options + the "X of Y"
  // total — same dedup-via-matching-query-key trick as Audit's optionsQ.
  const optionsScope = {
    window: f.range,
    start_date: scope.start_date,
    end_date: scope.end_date,
  };
  const optionsQ = useQuery({
    queryKey: ["usage", "summary", projectId, optionsScope],
    enabled: !!projectId,
    queryFn: () => api.getLlmUsageSummary(optionsScope, projectId as string),
  });
  const summaryQ = useQuery({
    queryKey: ["usage", "summary", projectId, scope],
    enabled: !!projectId,
    queryFn: () => api.getLlmUsageSummary(scope, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });

  const totals = summaryQ.data?.totals ?? {
    calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
  };
  const options = optionsQ.data;
  const scopeFiltered = !!(f.agent || f.model || f.user);
  const rangeTotal = options?.totals.calls ?? 0;

  if (projectScope.status === "no-project") {
    return <NoProjectEmptyState resource="LLM usage" />;
  }

  return (
    <div className="mx-auto max-w-[1400px]">
      <PageHeader
        title="Usage"
        description={
          totals.calls > 0 ? (
            <>
              <span className="font-semibold text-foreground">
                {fmtTokens(totals.total_tokens)} tokens
              </span>{" "}
              over {pluralise(totals.calls, "model call")}{" "}
              {rangePhrase(f.range, f.start_date)}
              {scopeFiltered ? (
                " matching these filters"
              ) : (
                <>
                  {" "}
                  for{" "}
                  <span className="font-mono text-foreground">
                    {projectName}
                  </span>
                </>
              )}
              .
            </>
          ) : (
            <>
              LLM token usage for project{" "}
              <span className="font-mono text-foreground">{projectName}</span>
            </>
          )
        }
        actions={<DashboardRangePicker f={f} setF={setF} />}
      />

      <UsageFilterBar
        f={f}
        setF={setF}
        shown={totals.calls}
        total={rangeTotal}
        agents={options?.by_agent.map((r) => r.key) ?? []}
        models={options?.by_model.map((r) => r.key) ?? []}
        users={options?.by_user.map((r) => displayNoValue(r).key) ?? []}
      />
      <UsageActiveChips f={f} setF={setF} />

      <div className="mb-4">
        <UsageStatStrip
          totals={totals}
          range={f.range}
          start_date={f.start_date}
          end_date={f.end_date}
        />
      </div>

      <UsageBreakdownCard
        byModel={summaryQ.data?.by_model ?? []}
        byAgent={summaryQ.data?.by_agent ?? []}
        byUser={summaryQ.data?.by_user.map(displayNoValue) ?? []}
        f={f}
        setF={setF}
      />
      {projectId && (
        <div className="mt-4">
          <TopUsersCard projectId={projectId} f={f} setF={setF} />
        </div>
      )}
    </div>
  );
}
