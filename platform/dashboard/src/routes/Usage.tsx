import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { DashboardRangePicker } from "@/components/ui/dashboard-range-picker";
import { StatStrip } from "@/components/ui/stat-strip";
import { displayNoValue, scopeNoValue } from "@/components/audit/chart-tokens";
import { rangeDays, rangePhrase } from "@/lib/date-range";
import {
  UsageActiveChips,
  UsageBreakdownCard,
  UsageFilterBar,
} from "@/components/usage/pieces";
import { TopUsersCard } from "@/components/usage/top-users";
import { useUsageFilters } from "@/lib/usage-filters";
import { PageHeader } from "@/components/ui/page-header";
import { fmtTokens } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Input and output token colours, shared by the tiles and the split bar. */
const INPUT_SWATCH = "bg-primary/40";
const OUTPUT_SWATCH = "bg-primary";

export function UsagePage() {
  const projectScope = useProjectScoped();
  const projectId = projectScope.projectId;
  const projectName = projectScope.projectName ?? projectId;

  const f = useUsageFilters((s) => s.filters);
  const setF = useUsageFilters((s) => s.setFilters);

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

  const nDays = rangeDays(f.range, f.start_date, f.end_date);
  const hourly = !f.start_date && f.range === "24h";
  // `fmt` overrides the plain count (e.g. compact tokens). en-US, like
  // fmtTokens beside it, so one strip never mixes decimal marks.
  const avgLabel = (
    count: number,
    fmt = (n: number) =>
      n.toLocaleString("en-US", { maximumFractionDigits: hourly ? 1 : 0 }),
  ) =>
    `${fmt(hourly ? count / 24 : count / nDays)}/${hourly ? "hr" : "day"} avg`;
  const inputPct = totals.total_tokens
    ? Math.round((totals.input_tokens / totals.total_tokens) * 100)
    : 0;
  const outputPct = totals.total_tokens
    ? Math.round((totals.output_tokens / totals.total_tokens) * 100)
    : 0;

  if (projectScope.status === "no-project") {
    return <NoProjectEmptyState resource="LLM usage" />;
  }

  return (
    <div className="mx-auto max-w-[1400px]">
      <PageHeader
        title="Usage"
        description={
          // The previous range's totals stay on screen while the new ones
          // load; don't word them as the new range's.
          summaryQ.isPlaceholderData || !summaryQ.data ? (
            <>
              LLM token usage for project{" "}
              <span className="font-mono text-foreground">{projectName}</span>
            </>
          ) : totals.calls > 0 ? (
            <>
              <span className="font-semibold text-foreground">
                {fmtTokens(totals.total_tokens)} tokens
              </span>{" "}
              over {totals.calls.toLocaleString()} model{" "}
              {totals.calls === 1 ? "call" : "calls"}{" "}
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
              No model calls {rangePhrase(f.range, f.start_date)}
              {/* Only blame the filters when the range has calls at all. */}
              {scopeFiltered && rangeTotal > 0 ? (
                " match these filters"
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

      {/* The previous range's numbers stay while the new ones load: dim them
          so they don't read as the new range's. */}
      <div
        className={cn(
          "mb-4 transition-opacity",
          summaryQ.isPlaceholderData && "opacity-50",
        )}
      >
        <StatStrip
          stats={[
            {
              key: "calls",
              label: "Model calls",
              value: totals.calls.toLocaleString(),
              sub: avgLabel(totals.calls),
            },
            {
              key: "input",
              label: "Input tokens",
              swatchClass: INPUT_SWATCH,
              value: fmtTokens(totals.input_tokens),
              sub: `${inputPct}% of total`,
            },
            {
              key: "output",
              label: "Output tokens",
              swatchClass: OUTPUT_SWATCH,
              value: fmtTokens(totals.output_tokens),
              sub: `${outputPct}% of total`,
            },
            {
              key: "total",
              label: "Total tokens",
              value: fmtTokens(totals.total_tokens),
              sub: avgLabel(totals.total_tokens, (n) =>
                fmtTokens(Math.round(n)),
              ),
            },
          ]}
          split={[
            {
              key: "input",
              value: totals.input_tokens,
              className: INPUT_SWATCH,
            },
            {
              key: "output",
              value: totals.output_tokens,
              className: OUTPUT_SWATCH,
            },
          ]}
        />
      </div>

      <div
        className={cn(
          "transition-opacity",
          summaryQ.isPlaceholderData && "opacity-50",
        )}
      >
        <UsageBreakdownCard
          byModel={summaryQ.data?.by_model ?? []}
          byAgent={summaryQ.data?.by_agent ?? []}
          byUser={summaryQ.data?.by_user.map(displayNoValue) ?? []}
          f={f}
          setF={setF}
        />
      </div>
      {projectId && (
        <div className="mt-4">
          <TopUsersCard projectId={projectId} f={f} setF={setF} />
        </div>
      )}
    </div>
  );
}
