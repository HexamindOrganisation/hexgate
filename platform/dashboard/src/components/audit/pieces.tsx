import { useMemo, useState, type ComponentType } from "react";
import { isToday, isYesterday } from "date-fns";
import {
  Ban,
  Check,
  ChevronRight,
  CircleDashed,
  Download,
  List,
  TriangleAlert,
  X,
} from "lucide-react";
import type {
  AnomalySeverity,
  AuditAnomaly,
  AuditBreakdownRow,
  AuditDecisionRow,
  AuditOutcome,
} from "@/lib/api";
import type {
  AuditFilters as Filters,
  SetAuditFilters as SetFilters,
} from "@/lib/audit-filters";
import { ActiveFilterChips } from "@/components/ui/active-filter-chips";
import { Callout } from "@/components/ui/callout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ALL, FilterSelect } from "@/components/ui/filter-select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Sparkline } from "@/components/ui/charts";
import { cn } from "@/lib/utils";
import { BreakdownCardShell } from "@/components/ui/breakdown-card";
import { BreakdownBar, type BreakdownDatum } from "./charts";
import { rankAnomalies } from "./anomalies";
import { argsPreview, fmtClock } from "./fmt";
import { OUT_LABEL, OUT_SWATCH, OUTCOME_SERIES } from "./chart-tokens";

// Map a server breakdown row ({key, all, ...}) to the chart datum ({total, ...}).
const toDatum = (r: AuditBreakdownRow): BreakdownDatum => ({
  key: r.key,
  total: r.all,
  allow: r.allow,
  deny: r.deny,
  needs_approval: r.needs_approval,
});

// ————————————————————————————————————————————— Filter bar
export function FilterBar({
  f,
  setF,
  shown,
  total,
  agents,
  roles,
  tools,
}: {
  f: Filters;
  setF: SetFilters;
  shown: number;
  total: number;
  agents: string[];
  roles: string[];
  tools: string[];
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
        value={f.role}
        all="All roles"
        opts={roles}
        onChange={(v) => set("role", v)}
      />
      <FilterSelect
        value={f.tool}
        all="All tools"
        opts={tools}
        onChange={(v) => set("tool", v)}
      />
      <span className="ml-1 text-xs text-muted-foreground">Outcome</span>
      <ToggleGroup
        type="single"
        value={f.outcome || ALL}
        onValueChange={(v) =>
          set("outcome", !v || v === ALL ? "" : (v as AuditOutcome))
        }
      >
        <ToggleGroupItem value={ALL}>
          <List className="size-3" />
          All
        </ToggleGroupItem>
        <ToggleGroupItem
          value="allow"
          className="text-allow data-[state=on]:text-allow"
        >
          <Check className="size-3" strokeWidth={2} />
          allow
        </ToggleGroupItem>
        <ToggleGroupItem
          value="deny"
          className="text-deny data-[state=on]:text-deny"
        >
          <X className="size-3" strokeWidth={2} />
          deny
        </ToggleGroupItem>
        <ToggleGroupItem
          value="needs_approval"
          className="text-approval data-[state=on]:text-approval"
        >
          <CircleDashed className="size-3" strokeWidth={2} />
          approval
        </ToggleGroupItem>
      </ToggleGroup>
      <span className="ml-auto whitespace-nowrap text-xs text-muted-foreground">
        <span className="text-foreground">{shown.toLocaleString()}</span> of{" "}
        <span className="font-mono">{total.toLocaleString()}</span> decisions
      </span>
    </div>
  );
}

const CHIP_KEYS: (keyof Filters)[] = [
  "agent",
  "role",
  "tool",
  "user",
  "outcome",
];

export function ActiveChips({ f, setF }: { f: Filters; setF: SetFilters }) {
  return (
    <ActiveFilterChips
      f={f}
      setF={setF}
      chipKeys={CHIP_KEYS}
      onClearAll={() =>
        setF((p) => ({
          ...p,
          agent: "",
          role: "",
          tool: "",
          outcome: "",
          customMode: false,
          start_date: null,
          end_date: null,
          user: "",
        }))
      }
    />
  );
}

// ————————————————————————————————————————————— KPI tile
export function KpiCard({
  label,
  icon: KpiIcon,
  value,
  sub,
  color,
  spark,
  sparkColor,
  showSpark,
  onClick,
  active,
}: {
  label: string;
  icon: ComponentType<{ className?: string }>;
  value: string;
  sub: string;
  color?: "allow" | "deny" | "approval";
  spark?: number[];
  sparkColor?: string;
  showSpark?: boolean;
  onClick?: () => void;
  active?: boolean;
}) {
  // Static map — Tailwind's scanner can't see interpolated class names.
  const valColor = {
    allow: "text-allow",
    deny: "text-deny",
    approval: "text-approval",
  };
  return (
    <Card
      onClick={onClick}
      className={`p-5 transition-colors ${onClick ? "cursor-pointer" : ""} ${active ? "border-primary/50" : ""}`}
    >
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <KpiIcon className="size-3.5" />
          <span>{label}</span>
        </div>
        <div
          className={`text-[28px] font-semibold leading-tight tracking-tight ${color ? valColor[color] : ""}`}
        >
          {value}
        </div>
        <div className="text-caption text-muted-foreground">{sub}</div>
        {showSpark && spark && (
          <div className="mt-1.5">
            <Sparkline data={spark} color={sparkColor} width={170} />
          </div>
        )}
      </div>
    </Card>
  );
}

// ————————————————————————————————————————————— Breakdown card
const DIMS = [
  { id: "tool" as const, label: "Tools", fkey: "tool" as const },
  { id: "agent" as const, label: "Agents", fkey: "agent" as const },
  { id: "role" as const, label: "Caller roles", fkey: "role" as const },
];

export function BreakdownCard({
  byTool,
  byAgent,
  byRole,
  f,
  setF,
}: {
  byTool: AuditBreakdownRow[];
  byAgent: AuditBreakdownRow[];
  byRole: AuditBreakdownRow[];
  f: Filters;
  setF: SetFilters;
}) {
  const [dim, setDim] = useState<"tool" | "agent" | "role">("tool");
  const [sort, setSort] = useState<"volume" | "denials">("volume");
  const source = dim === "tool" ? byTool : dim === "agent" ? byAgent : byRole;
  const data = useMemo(() => {
    let d = source.map(toDatum);
    if (sort === "denials")
      d = d.slice().sort((a, b) => b.deny - a.deny || b.total - a.total);
    return d.slice(0, 10);
  }, [source, sort]);
  const max = Math.max(...data.map((d) => d.total), 1);
  const fkey = dim;

  return (
    <BreakdownCardShell
      dims={DIMS}
      dim={dim}
      onDimChange={setDim}
      metric={sort}
      metricOptions={[
        { value: "volume", label: "by volume" },
        { value: "denials", label: "by denials" },
      ]}
      onMetricChange={setSort}
      columns={1}
      emptyMessage="No decisions match."
      legend={
        <>
          {OUTCOME_SERIES.map((s) => (
            <span key={s.key} className="flex items-center gap-1.5">
              <span className={`size-2 rounded-sm ${s.swatchClass}`} />
              {s.label}
            </span>
          ))}
          {/* Stated inline, not in a tooltip: these bars legitimately sum
              above the total decision count. Dropped once a role filter is
              active — the server then collapses this to the filtered role, so
              nothing multi-counts and the caveat would mislead. */}
          {dim === "role" && !f[fkey] && (
            <span className="italic">
              counts role membership — a caller with several roles counts under
              each
            </span>
          )}
        </>
      }
      rows={data}
      activeFilterValue={f[fkey]}
      onFilterChange={(v) => setF((p) => ({ ...p, [fkey]: v }))}
      renderRow={(row, { active, onClick }) => (
        <BreakdownBar
          label={row.key}
          row={row}
          max={max}
          active={active}
          onClick={onClick}
        />
      )}
    />
  );
}

/**
 * One row's caller roles on a single line: first name plus `+N`. The column
 * stays narrow; the full set is on hover, and in the drawer.
 */
function RolesCell({ roles }: { roles: string[] }) {
  if (!roles.length) return <>—</>;
  return (
    <span title={roles.join(", ")}>
      {roles[0]}
      {roles.length > 1 && (
        <span className="ml-1 text-muted-foreground">+{roles.length - 1}</span>
      )}
    </span>
  );
}

// ————————————————————————————————————————————— Events table
const OUTCOME_TEXT: Record<AuditOutcome, string> = {
  deny: "text-deny",
  needs_approval: "text-approval",
  allow: "text-muted-foreground",
};

/** Swatch + label; the log's quiet alternative to the pill badge. */
export function OutcomeLabel({ d }: { d: AuditOutcome }) {
  return (
    <span
      className={cn("flex items-center gap-1.5 font-semibold", OUTCOME_TEXT[d])}
    >
      <span className={cn("size-2 shrink-0 rounded-sm", OUT_SWATCH[d])} />
      {OUT_LABEL[d]}
    </span>
  );
}

function dayLabel(d: Date): string {
  const date = d.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
  return isToday(d)
    ? `Today · ${date}`
    : isYesterday(d)
      ? `Yesterday · ${date}`
      : date;
}

const LOG_COLS =
  "grid grid-cols-[96px_92px_minmax(0,1.5fr)_120px_96px_minmax(0,1.6fr)_16px] items-center gap-3.5";

export function EventsTable({
  rows,
  total,
  onSelect,
  selectedId,
  onLoadMore,
  loadingMore,
  onExport,
}: {
  rows: AuditDecisionRow[];
  total: number;
  onSelect: (e: AuditDecisionRow) => void;
  selectedId?: string | null;
  onLoadMore: () => void;
  loadingMore?: boolean;
  onExport?: () => void;
}) {
  // Rows arrive newest first, so consecutive runs share a day.
  const groups: { label: string; rows: AuditDecisionRow[] }[] = [];
  let lastDay = "";
  for (const r of rows) {
    const d = new Date(r.occurred_at);
    const day = d.toDateString();
    if (day === lastDay) groups.at(-1)!.rows.push(r);
    else groups.push({ label: dayLabel(d), rows: [r] });
    lastDay = day;
  }
  return (
    <Card className="overflow-hidden p-0">
      <div className="flex flex-wrap items-center gap-3 border-b border-border px-5 py-4">
        <h2 className="text-body font-semibold">Decision log</h2>
        <span className="text-meta text-muted-foreground">
          {total.toLocaleString()} decisions · newest first. Click a row to
          inspect.
        </span>
        <Button
          variant="outline"
          size="sm"
          className="ml-auto gap-1.5"
          onClick={onExport}
          disabled={!onExport}
        >
          <Download className="size-3.5" />
          Export JSONL
        </Button>
      </div>
      <div className="overflow-x-auto scrollbar-thin">
        <div className="min-w-[940px]">
          <div
            className={cn(
              LOG_COLS,
              "border-b border-border px-5 py-2.5 text-caption font-semibold uppercase tracking-wider text-muted-foreground",
            )}
          >
            <span>Time</span>
            <span>Outcome</span>
            <span>Tool</span>
            <span>Agent</span>
            <span>Role</span>
            <span>Reason</span>
            <span />
          </div>
          {groups.map((g) => (
            <div key={g.label}>
              <div className="px-5 pb-1.5 pt-3.5 text-xs font-semibold text-muted-foreground">
                {g.label}
              </div>
              {g.rows.map((e) => (
                <button
                  key={e.event_id}
                  type="button"
                  onClick={() => onSelect(e)}
                  aria-current={selectedId === e.event_id || undefined}
                  className={cn(
                    LOG_COLS,
                    "w-full border-t border-border/60 px-5 py-2.5 text-left text-meta transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                    selectedId === e.event_id &&
                      "bg-primary/10 hover:bg-primary/10",
                  )}
                >
                  <span className="font-mono text-xs text-muted-foreground">
                    {fmtClock(new Date(e.occurred_at))}
                  </span>
                  <OutcomeLabel d={e.outcome} />
                  <span className="truncate font-mono">
                    {e.tool_name}
                    <span className="text-muted-foreground">
                      {argsPreview(e.arguments)}
                    </span>
                  </span>
                  <span className="truncate font-mono text-xs">
                    {e.agent_name}
                  </span>
                  <span
                    className={cn(
                      "truncate",
                      !e.user_roles.length && "text-muted-foreground",
                    )}
                  >
                    <RolesCell roles={e.user_roles} />
                  </span>
                  <span
                    className={cn(
                      "truncate",
                      e.outcome === "allow" && "text-muted-foreground",
                    )}
                  >
                    {e.reason || "—"}
                  </span>
                  <ChevronRight className="size-3.5 text-muted-foreground" />
                </button>
              ))}
            </div>
          ))}
          {!rows.length && (
            <div className="py-10 text-center text-meta text-muted-foreground">
              No decisions match the current filters.
            </div>
          )}
        </div>
      </div>
      {rows.length < total && (
        <div className="border-t border-border px-5 py-3 text-center">
          <Button
            variant="outline"
            size="sm"
            onClick={onLoadMore}
            disabled={loadingMore}
          >
            {loadingMore
              ? "Loading…"
              : `Load 40 more · ${(total - rows.length).toLocaleString()} remaining`}
          </Button>
        </div>
      )}
    </Card>
  );
}

// ————————————————————————————————————————————— Anomalies card

function fmtBurst(first: string, last: string): string {
  const f = new Date(first);
  const l = new Date(last);
  const date = (d: Date) =>
    d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  const time = (d: Date) =>
    d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
  return date(f) === date(l)
    ? `${date(f)}, ${time(f)} → ${time(l)}`
    : `${date(f)}, ${time(f)} → ${date(l)}, ${time(l)}`;
}

function SeverityBadge({ severity }: { severity: AnomalySeverity }) {
  return severity === "high" ? (
    <Badge className="bg-deny/15 text-deny hover:bg-deny/15">High</Badge>
  ) : (
    <Badge className="bg-approval/15 text-approval hover:bg-approval/15">
      Medium
    </Badge>
  );
}

// The endpoint returns every burst; show the worst few up front.
const ANOMALIES_SHOWN = 3;

export function AnomaliesCard({
  rows,
  onRowClick,
  onBanUser,
}: {
  rows: AuditAnomaly[];
  onRowClick: (from: Date, to: Date, userId: string) => void;
  /** When set, each row gets a "Ban user" action (admins only — the caller
   * gates on role). Undefined hides the action entirely. */
  onBanUser?: (userId: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const ranked = useMemo(() => rankAnomalies(rows), [rows]);
  const shown = expanded ? ranked : ranked.slice(0, ANOMALIES_SHOWN);
  return (
    <section aria-label="Anomalies" className="space-y-2">
      <div className="flex items-center gap-3 text-caption font-semibold uppercase tracking-wider text-muted-foreground">
        Anomalies · {rows.length} detected
        {rows.length > ANOMALIES_SHOWN && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="normal-case tracking-normal text-primary hover:underline"
          >
            {expanded
              ? "Show fewer"
              : `Show ${rows.length - ANOMALIES_SHOWN} more`}
          </button>
        )}
      </div>
      {shown.map((row) => {
        const inspect = () =>
          onRowClick(
            new Date(row.first_seen),
            new Date(row.last_seen),
            row.user_id,
          );
        return (
          <Callout
            key={`${row.user_id}-${row.first_seen}`}
            icon={<TriangleAlert />}
            title={
              <span className="flex flex-wrap items-center gap-2">
                Denial burst from
                <button
                  type="button"
                  title="Filter the page to this burst"
                  onClick={inspect}
                  className="font-mono font-medium underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {row.user_id}
                </button>
                <SeverityBadge severity={row.severity} />
              </span>
            }
            actions={
              <>
                <Button variant="outline" size="sm" onClick={inspect}>
                  Inspect
                </Button>
                {onBanUser && (
                  <Button
                    variant="outline"
                    size="sm"
                    className="gap-1.5 text-destructive hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => onBanUser(row.user_id)}
                  >
                    <Ban className="size-3.5" />
                    Ban user
                  </Button>
                )}
              </>
            }
          >
            {row.deny} of {row.all} calls denied ·{" "}
            <span>{(row.deny_rate * 100).toFixed(0)}%</span> deny rate ·{" "}
            <span className="font-mono">
              {fmtBurst(row.first_seen, row.last_seen)}
            </span>
          </Callout>
        );
      })}
    </section>
  );
}
