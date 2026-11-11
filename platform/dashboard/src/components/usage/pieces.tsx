import { useMemo, useState } from "react";
import type {
  LlmInvocationBreakdownRow,
  LlmInvocationSummary,
} from "@/lib/api";
import type {
  SetUsageFilters as SetFilters,
  UsageFilters as Filters,
} from "@/lib/usage-filters";
import { ActiveFilterChips } from "@/components/ui/active-filter-chips";
import { BarRow, BreakdownCardShell } from "@/components/ui/breakdown-card";
import { FilterSelect } from "@/components/ui/filter-select";
import { StatStrip } from "@/components/ui/stat-strip";
import { type Range, rangeDays } from "@/lib/date-range";
import { fmtTokens } from "@/lib/format";
import { pickDims } from "@/lib/breakdown-dims";

// ————————————————————————————————————————————— Filter bar
export function UsageFilterBar({
  f,
  setF,
  shown,
  total,
  agents,
  models,
  users,
}: {
  f: Filters;
  setF: SetFilters;
  shown: number;
  total: number;
  agents: string[];
  models: string[];
  users: string[];
}) {
  const set = <K extends keyof Filters>(k: K, v: Filters[K]) =>
    setF((p) => ({ ...p, [k]: v }));
  return (
    <div className="mb-3.5 flex flex-wrap items-center gap-2">
      <FilterSelect
        value={f.agent}
        all="All agents"
        opts={agents}
        onChange={(v) => set("agent", v)}
      />
      <FilterSelect
        value={f.model}
        all="All models"
        opts={models}
        onChange={(v) => set("model", v)}
      />
      <FilterSelect
        value={f.user}
        all="All users"
        opts={users}
        onChange={(v) => set("user", v)}
      />
      <span className="ml-auto whitespace-nowrap text-xs text-muted-foreground">
        <span className="text-foreground">{shown.toLocaleString()}</span> of{" "}
        <span className="font-mono">{total.toLocaleString()}</span> LLM calls
      </span>
    </div>
  );
}

const CHIP_KEYS: (keyof Filters)[] = ["agent", "model", "user"];

export function UsageActiveChips({
  f,
  setF,
}: {
  f: Filters;
  setF: SetFilters;
}) {
  return (
    <ActiveFilterChips
      f={f}
      setF={setF}
      chipKeys={CHIP_KEYS}
      onClearAll={() =>
        setF((p) => ({
          ...p,
          agent: "",
          model: "",
          user: "",
          customMode: false,
          start_date: null,
          end_date: null,
        }))
      }
    />
  );
}

// ————————————————————————————————————————————— Breakdown bar (one volume
// value per row, no outcome mix — unlike audit/charts.tsx's BreakdownBar)
export function UsageBar({
  label,
  value,
  max,
  active,
  onClick,
}: {
  label: string;
  value: number;
  max: number;
  active?: boolean;
  onClick?: () => void;
}) {
  const widthPct = max ? (value / max) * 100 : 0;
  return (
    <BarRow
      onClick={onClick}
      active={active}
      widthPct={widthPct}
      header={
        <>
          <span className="truncate font-mono">{label}</span>
          <span className="shrink-0 text-foreground">
            {value.toLocaleString()}
          </span>
        </>
      }
      segments={[{ className: "bg-primary", widthPct: 100 }]}
    />
  );
}

// ————————————————————————————————————————————— Breakdown card
const DIMS = [
  { id: "model" as const, label: "Models" },
  { id: "agent" as const, label: "Agents" },
  { id: "user" as const, label: "Users" },
];
type UsageDim = (typeof DIMS)[number]["id"];

export function UsageBreakdownCard({
  byModel = [],
  byAgent = [],
  byUser = [],
  f,
  setF,
  dims,
}: {
  byModel?: LlmInvocationBreakdownRow[];
  byAgent?: LlmInvocationBreakdownRow[];
  byUser?: LlmInvocationBreakdownRow[];
  f: Filters;
  setF: SetFilters;
  /** The dimensions to offer, in this order (default: all); the first is
   * selected. */
  dims?: [UsageDim, ...UsageDim[]];
}) {
  const shownDims = pickDims(DIMS, dims);
  const [dim, setDim] = useState<UsageDim>(shownDims[0].id);
  const [metric, setMetric] = useState<"total_tokens" | "calls">(
    "total_tokens",
  );
  const source = dim === "model" ? byModel : dim === "agent" ? byAgent : byUser;
  const data = useMemo(() => {
    // The card shows the first 10 and offers the rest.
    return source.slice().sort((a, b) => b[metric] - a[metric]);
  }, [source, metric]);
  const max = Math.max(...data.map((d) => d[metric]), 1);
  const fkey = dim;

  return (
    <BreakdownCardShell
      dims={shownDims}
      dim={dim}
      onDimChange={setDim}
      metric={metric}
      metricOptions={[
        { value: "total_tokens", label: "by tokens" },
        { value: "calls", label: "by calls" },
      ]}
      onMetricChange={setMetric}
      emptyMessage="No model calls match."
      legend={
        <span className="flex items-center gap-1.5">
          <span className="size-2 rounded-sm bg-primary" />
          {metric === "total_tokens" ? "total tokens" : "calls"}
        </span>
      }
      rows={data}
      activeFilterValue={f[fkey]}
      onFilterChange={(v) => setF((p) => ({ ...p, [fkey]: v }))}
      renderRow={(row, { active, onClick }) => (
        <UsageBar
          label={row.key}
          value={row[metric]}
          max={max}
          active={active}
          onClick={onClick}
        />
      )}
    />
  );
}

// ————————————————————————————————————————————— Stat strip
/** Input and output token colours, shared by the tiles and the split bar. */
const INPUT_SWATCH = "bg-primary/40";
const OUTPUT_SWATCH = "bg-primary";

/** Model calls, input / output / total tokens, with per-hour or per-day
 * averages. Shared by the Usage page and the agent hub's Usage tab. */
export function UsageStatStrip({
  totals,
  range,
  start_date,
  end_date,
}: {
  totals: LlmInvocationSummary["totals"];
  range: Range;
  start_date: Date | null;
  end_date: Date | null;
}) {
  const nDays = rangeDays(range, start_date, end_date);
  const hourly = !start_date && range === "24h";
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
  return (
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
          sub: avgLabel(totals.total_tokens, (n) => fmtTokens(Math.round(n))),
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
  );
}
