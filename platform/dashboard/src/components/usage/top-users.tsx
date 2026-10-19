import { useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type TopUsersWindow, type UsageMetric } from "@/lib/api";
import type { SetUsageFilters, UsageFilters } from "@/lib/usage-filters";
import { BarRow, BreakdownCardShell } from "@/components/ui/breakdown-card";

const TOP_USERS_LIMIT = 10;
// Half the summary's rate: each refetch can be a 30-day GROUP BY.
const TOP_USERS_REFETCH_MS = 60_000;

// Labels must not collide with the page's range picker ("7d") or the
// breakdown card's metric select ("by tokens").
const WINDOWS: { id: TopUsersWindow; label: string }[] = [
  { id: "1h", label: "1 hour" },
  { id: "24h", label: "24 hours" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
];

// Input and output tokens stay out: the KPIs above already split them.
type TopUsersMetric = Exclude<UsageMetric, "input_tokens" | "output_tokens">;
const METRIC_LABELS: Record<TopUsersMetric, string> = {
  total_tokens: "total tokens",
  invocations: "invocations",
  tool_calls: "tool calls",
  denials: "denials",
  llm_calls: "LLM calls",
};
const METRIC_OPTIONS = (
  Object.entries(METRIC_LABELS) as [TopUsersMetric, string][]
).map(([value, label]) => ({ value, label: `by ${label}` }));

const LOADING_MESSAGE = "Loading…";
const EMPTY_MESSAGE = "No user activity in this window.";
const UNAVAILABLE_MESSAGE = "Top users are unavailable right now.";

type Row = { key: string; value: number };

function TopUserBar({
  row,
  max,
  active,
  onClick,
}: {
  row: Row;
  max: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <BarRow
      onClick={onClick}
      active={active}
      widthPct={max ? (row.value / max) * 100 : 0}
      header={
        <>
          <span className="truncate font-mono">{row.key}</span>
          <span className="shrink-0 text-foreground">
            {row.value.toLocaleString()}
          </span>
        </>
      }
      segments={[{ className: "bg-primary", widthPct: 100 }]}
    />
  );
}

export function TopUsersCard({
  projectId,
  f,
  setF,
}: {
  projectId: string;
  f: UsageFilters;
  setF: SetUsageFilters;
}) {
  const [window, setWindow] = useState<TopUsersWindow>("24h");
  const [metric, setMetric] = useState<TopUsersMetric>("total_tokens");
  const q = useQuery({
    queryKey: ["usage", "top-users", projectId, { window, metric }],
    queryFn: () =>
      api.getTopUsers({ window, metric, limit: TOP_USERS_LIMIT }, projectId),
    placeholderData: keepPreviousData,
    refetchInterval: TOP_USERS_REFETCH_MS,
  });

  // Already ranked and limited by the server: no client-side sort.
  const rows: Row[] = q.isError
    ? []
    : (q.data?.users ?? []).map((u) => ({
        key: u.user_id,
        value: u.value,
      }));
  const max = rows[0]?.value ?? 0;

  return (
    <section aria-label="Top users">
      <h2 className="text-lg font-semibold tracking-tight">Top users</h2>
      <p className="mb-3 text-sm text-muted-foreground">
        Across all agents · page filters don't apply · calls without a user_id
        aren't counted
      </p>
      <BreakdownCardShell
        dims={WINDOWS}
        dim={window}
        onDimChange={setWindow}
        metric={metric}
        metricOptions={METRIC_OPTIONS}
        onMetricChange={setMetric}
        emptyMessage={
          q.isError
            ? UNAVAILABLE_MESSAGE
            : q.isPending
              ? LOADING_MESSAGE
              : EMPTY_MESSAGE
        }
        legend={
          <span className="flex items-center gap-1.5">
            <span className="size-2 rounded-sm bg-primary" />
            {METRIC_LABELS[metric]} per user
          </span>
        }
        rows={rows}
        activeFilterValue={f.user}
        onFilterChange={(v) => setF((p) => ({ ...p, user: v }))}
        renderRow={(row, { active, onClick }) => (
          <TopUserBar row={row} max={max} active={active} onClick={onClick} />
        )}
      />
    </section>
  );
}
