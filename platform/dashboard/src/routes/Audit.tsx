import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Filter, Lightbulb, X } from "lucide-react";
import { endOfDay, startOfDay } from "date-fns";
import { useNavigate } from "react-router-dom";
import {
  api,
  type AuditDecisionRow,
  type AuditOutcome,
  LLM_MESSAGE_PAGE,
} from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { useCanManageBans } from "@/lib/bans";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { DashboardRangePicker } from "@/components/ui/dashboard-range-picker";
import { type ChartBucket, StackedBars } from "@/components/ui/charts";
import { StatStrip } from "@/components/ui/stat-strip";
import { AuditOnboarding } from "@/components/audit/onboarding";
import { type Counts, DecisionBadge } from "@/components/audit/charts";
import {
  displayNoValue,
  OUT_LABEL,
  OUTCOME_SERIES,
  scopeNoValue,
} from "@/components/audit/chart-tokens";
import {
  type AuditFilters as Filters,
  useAuditFilters,
} from "@/lib/audit-filters";
import { rangeDays } from "@/lib/date-range";
import { fmtTs } from "@/components/audit/fmt";
import { LlmMessagesSection } from "@/components/audit/messages";
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
  return `${minutes} minutes`;
}

const fmtFull = (d: Date) =>
  d.toISOString().replace("T", " ").replace("Z", " UTC");

// ————————————————————————————————————————————— Detail drawer
function KV({
  k,
  children,
  mono,
  muted,
}: {
  k: string;
  children: React.ReactNode;
  mono?: boolean;
  muted?: boolean;
}) {
  return (
    <div className="grid grid-cols-[116px_1fr] items-baseline gap-2.5 py-[5px] text-meta">
      <div className="text-muted-foreground">{k}</div>
      <div
        className={`break-words ${mono ? "font-mono text-xs" : ""} ${muted ? "text-muted-foreground" : "text-foreground"}`}
      >
        {children}
      </div>
    </div>
  );
}
function DrawerSection({
  label,
  children,
  accent,
}: {
  label: string;
  children: React.ReactNode;
  accent?: string;
}) {
  return (
    <div className="mt-[22px]">
      <div
        className={`mb-2 text-[11px] font-medium uppercase tracking-wider ${accent || "text-muted-foreground"}`}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

// Per-outcome drawer accents (reason box + error text).
const TONE: Record<AuditOutcome, { text: string; box: string }> = {
  allow: { text: "text-allow", box: "border-allow/25 bg-allow/10" },
  deny: { text: "text-deny", box: "border-deny/25 bg-deny/10" },
  needs_approval: {
    text: "text-approval",
    box: "border-approval/25 bg-approval/10",
  },
};

function DetailDrawer({
  event,
  related,
  projectId,
  onClose,
  onSelect,
  setF,
}: {
  event: AuditDecisionRow | null;
  related: AuditDecisionRow[];
  projectId: string | null;
  onClose: () => void;
  onSelect: (e: AuditDecisionRow) => void;
  setF: (u: (p: Filters) => Filters) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Both scopes go out exactly as the row holds them, blanks included — see
  // api.listLlmMessages. Two blanks name no transcript, so the query is only
  // enabled when at least one is set.
  const scoped = !!(event?.session_id || event?.run_id);
  // Keyed on the decision, not just the session: a page is a window on the
  // transcript and which window we want depends on when the decision
  // happened, so two decisions far apart in one long session need two
  // fetches. Short transcripts — the overwhelming majority — come back whole
  // on the first request and every decision in them reuses that one entry.
  const messagesQ = useQuery({
    queryKey: [
      "audit",
      "llm-messages",
      projectId,
      event?.session_id,
      event?.run_id,
      event?.occurred_at,
    ],
    enabled: !!projectId && scoped,
    // One attempt, against the default of three. Every other read here is a
    // kilobyte of decision rows; this one can page over a transcript of
    // ~272 KiB rows with an uncapped offset, and ClickHouse runs it against
    // a server-wide memory budget shared with the enricher's inserts. Three
    // silent retries turn one expensive read into four, on a section the
    // rest of the drawer does not depend on.
    retry: 1,
    queryFn: async () => {
      const args = [
        projectId as string,
        event!.session_id,
        event!.run_id,
      ] as const;
      const head = await api.listLlmMessages(...args);
      // Rows are oldest-first, so a truncated page is the transcript's HEAD
      // and the turns after it are the ones missing. When the decision is
      // later than every row we got, its anchor is in that missing tail —
      // keeping the head would silently highlight some earlier call as "this
      // call". Take the last window instead, which is where a decision late
      // in a long conversation sits.
      const last = head.rows[head.rows.length - 1];
      if (
        head.total > head.rows.length &&
        last &&
        new Date(last.occurred_at) <= new Date(event!.occurred_at)
      ) {
        return api.listLlmMessages(
          ...args,
          LLM_MESSAGE_PAGE,
          head.total - LLM_MESSAGE_PAGE,
        );
      }
      return head;
    },
  });
  if (!event) return null;
  const e = event;
  const argStr =
    e.arguments == null ? "—" : JSON.stringify(e.arguments, null, 2);
  const attrStr =
    e.attributes == null ? null : JSON.stringify(e.attributes, null, 2);
  const hintStr =
    typeof e.hint === "string"
      ? e.hint
      : e.hint == null
        ? ""
        : JSON.stringify(e.hint);
  const tone = TONE[e.outcome];

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        onClick={onClose}
        className="absolute inset-0 bg-black/55 backdrop-blur-[1px]"
      />
      <aside className="relative flex h-full w-[472px] max-w-[92vw] flex-col border-l border-border bg-card shadow-2xl">
        <div className="flex items-center gap-2.5 border-b border-border px-5 py-4">
          <DecisionBadge d={e.outcome} />
          <span className="flex-1 truncate font-mono text-xs text-muted-foreground">
            {e.event_id}
          </span>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            title="Close (Esc)"
          >
            <X className="size-4" />
          </Button>
        </div>

        <div className="flex-1 overflow-auto px-5 pb-6 pt-1 scrollbar-thin">
          <div className="mt-[18px] flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-[17px] font-semibold text-foreground">
              {e.tool_name}
            </span>
            <span className="text-[13px] text-muted-foreground">by</span>
            <span className="font-mono text-sm text-foreground">
              {e.agent_name}
            </span>
          </div>
          <div
            className={`mt-2 rounded-lg border px-3 py-2.5 text-[13px] leading-relaxed text-foreground ${tone.box}`}
          >
            {e.reason ||
              (e.outcome === "allow"
                ? "Allowed — no rule violated."
                : "No reason recorded.")}
            {e.error_type && (
              <span
                className={`mt-1.5 block font-mono text-caption ${tone.text}`}
              >
                error_type: {e.error_type}
              </span>
            )}
          </div>

          {e.violations.length > 0 && (
            <DrawerSection label="Violations" accent="text-deny">
              <div className="flex flex-wrap gap-1.5">
                {e.violations.map((v) => (
                  <span
                    key={v}
                    className="rounded-md bg-deny/15 px-2 py-[3px] font-mono text-caption text-deny"
                  >
                    {v}
                  </span>
                ))}
              </div>
            </DrawerSection>
          )}

          {hintStr && (
            <DrawerSection label="Policy hint">
              <div className="flex items-center gap-2 rounded-md border border-border bg-muted px-3 py-2.5 font-mono text-xs">
                <Lightbulb className="size-3.5 shrink-0 text-approval" />
                <span className="text-foreground">{hintStr}</span>
              </div>
            </DrawerSection>
          )}

          <DrawerSection label="Decision">
            <KV k="outcome">{OUT_LABEL[e.outcome]}</KV>
            <KV k="tool_name" mono>
              {e.tool_name}
            </KV>
            {/* Two rows, not three: the legacy scalar `role` would say almost
                the same thing as "roles evaluated". */}
            <KV k="roles evaluated">
              {e.user_roles.length ? (
                e.user_roles.join(", ")
              ) : (
                <span className="text-muted-foreground">∅ none</span>
              )}
            </KV>
            {/* An empty deciding_role means three different things on the wire:
                every role denied, no roles evaluated at all (the enforcer ran
                without a HexgateContext, so `default` decided), or a pre-multi-
                role SDK that only sent the scalar. outcome + user_roles tell
                them apart — without this branch an allow reads as a denial. */}
            <KV k="granted by">
              {e.deciding_role || (
                <span className="text-muted-foreground">
                  {e.outcome === "deny"
                    ? "∅ none — no role granted it"
                    : e.user_roles.length === 0
                      ? "default policy — no roles evaluated"
                      : "not recorded (legacy event)"}
                </span>
              )}
            </KV>
            {e.error_type && (
              <KV k="error_type" mono>
                {e.error_type}
              </KV>
            )}
          </DrawerSection>

          <DrawerSection label="Arguments">
            <pre className="m-0 whitespace-pre-wrap rounded-lg border border-border bg-muted p-3 font-mono text-caption leading-relaxed text-foreground">
              {argStr}
            </pre>
          </DrawerSection>

          <DrawerSection label="Messages">
            <LlmMessagesSection
              rows={messagesQ.data?.rows ?? []}
              total={messagesQ.data?.total ?? 0}
              decisionOccurredAt={e.occurred_at}
              offset={messagesQ.data?.offset ?? 0}
              scoped={scoped}
              // The same condition as `enabled`, not a subset of it: a
              // disabled query stays `pending` forever, so a clause missing
              // here would show "Loading…" with no request in flight.
              isLoading={messagesQ.isPending && !!projectId && scoped}
              isError={messagesQ.isError}
            />
          </DrawerSection>

          {attrStr && (
            <DrawerSection label="Context attributes">
              <pre className="m-0 whitespace-pre-wrap rounded-lg border border-border bg-muted p-3 font-mono text-caption leading-relaxed text-foreground">
                {attrStr}
              </pre>
            </DrawerSection>
          )}

          <DrawerSection label="Envelope">
            <KV k="occurred_at" mono>
              {fmtFull(new Date(e.occurred_at))}
            </KV>
            <KV k="received_at" mono muted>
              {fmtFull(new Date(e.received_at))}
            </KV>
            <KV k="agent_name" mono>
              {e.agent_name}
            </KV>
            <KV k="session_id" mono muted>
              {e.session_id || "∅"}
            </KV>
            <KV k="user_id" mono muted>
              {e.user_id || "∅"}
            </KV>
          </DrawerSection>

          <DrawerSection label={`Same session · ${related.length}`}>
            {related.length ? (
              <div className="overflow-hidden rounded-lg border border-border">
                {related.map((r, i) => (
                  <div
                    key={r.event_id}
                    onClick={() => onSelect(r)}
                    className={`flex cursor-pointer items-center gap-2 px-2.5 py-2 text-xs hover:bg-accent ${i < related.length - 1 ? "border-b border-border" : ""}`}
                  >
                    <DecisionBadge d={r.outcome} />
                    <span className="flex-1 truncate font-mono">
                      {r.tool_name}
                    </span>
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {fmtTs(new Date(r.occurred_at))}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-muted-foreground">
                No other events in this session.
              </div>
            )}
          </DrawerSection>
        </div>

        <div className="flex gap-2 border-t border-border px-5 py-3">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setF((p) => ({ ...p, agent: e.agent_name }));
              onClose();
            }}
          >
            <Filter className="size-3" />
            Filter to agent
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setF((p) => ({ ...p, tool: e.tool_name }));
              onClose();
            }}
          >
            <Filter className="size-3" />
            Filter to tool
          </Button>
        </div>
      </aside>
    </div>
  );
}

// ————————————————————————————————————————————— Root
export function AuditPage() {
  const projectScope = useProjectScoped();
  const projectId = projectScope.projectId;
  const navigate = useNavigate();
  const canManageBans = useCanManageBans();
  // Display name for the page subtitle; falls back to the id.
  const projectName = projectScope.projectName ?? projectId;
  // Filter + paging state lives in a zustand store (see lib/audit-filters)
  // so the dialled-in slice survives route switches; drawer selection is
  // ephemeral and stays local.
  const f = useAuditFilters((s) => s.filters);
  const setF = useAuditFilters((s) => s.setFilters);
  const tableLimit = useAuditFilters((s) => s.tableLimit);
  const loadMore = useAuditFilters((s) => s.loadMore);
  const [sel, setSel] = useState<AuditDecisionRow | null>(null);

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
  const listQ = useQuery({
    queryKey: ["audit", "list", projectId, listFilters],
    enabled: !!projectId,
    queryFn: () => api.listAuditDecisions(listFilters, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  const relatedQ = useQuery({
    queryKey: ["audit", "session", projectId, f.range, sel?.session_id],
    enabled: !!sel?.session_id && !!projectId,
    queryFn: () =>
      api.listAuditDecisions(
        { window: f.range, session_id: sel!.session_id, limit: 12 },
        projectId as string,
      ),
  });
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

  // The server sizes buckets by span (e.g. 6 h over 7 days), so read the
  // width off the data rather than the preset.
  const bucketMinutes = useMemo(() => {
    const d = tsQ.data;
    if (!d || d.length < 2) return 1440;
    return Math.round((+new Date(d[1].bucket) - +new Date(d[0].bucket)) / 6e4);
  }, [tsQ.data]);
  const days: ChartBucket[] = useMemo(
    () =>
      (tsQ.data ?? []).map((p) => ({
        label: new Date(p.bucket),
        mode: bucketMinutes >= 1440 ? "day" : "hour",
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
  const hasFilters = !!(
    f.agent ||
    f.role ||
    f.tool ||
    f.user ||
    f.outcome ||
    f.start_date
  );
  // Nothing recorded in the range and nothing filtered out: show next steps
  // instead of a wall of zeros.
  const showOnboarding = optionsQ.isSuccess && rangeTotal === 0 && !hasFilters;
  const rangePhrase = f.start_date
    ? "in the selected range"
    : {
        "24h": "in the last 24 hours",
        "7d": "in the last 7 days",
        "30d": "in the last 30 days",
        "90d": "in the last 90 days",
      }[f.range];
  const related = (relatedQ.data?.rows ?? [])
    .filter((r) => r.event_id !== sel?.event_id)
    .slice(0, 6);

  // Exports the LOADED page only (up to tableLimit ≤ 200 rows), not every
  // row matching the filters — "export everything" would need a streaming
  // server-side endpoint, not a client-side blob.
  const exportJsonl = () => {
    const rows = listQ.data?.rows ?? [];
    if (!rows.length) return;
    const blob = new Blob([rows.map((r) => JSON.stringify(r)).join("\n")], {
      type: "application/x-ndjson",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "decisions.jsonl";
    a.click();
    URL.revokeObjectURL(url);
  };

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
                  ? `decisions match these filters ${rangePhrase}`
                  : `policy decisions ${rangePhrase} for `}
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
          shown={listQ.data?.total ?? 0}
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
                  swatchClass: "bg-deny",
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
                  swatchClass: "bg-approval",
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
                  swatchClass: "bg-allow-fill",
                  value: counts.allow.toLocaleString(),
                  sub: `${allowPct}% of decisions`,
                  spark: spark.allow,
                  sparkColor: "hsl(var(--semantic-allow))",
                  onClick: () => setOutcome("allow"),
                  active: f.outcome === "allow",
                },
              ]}
              split={[
                { key: "deny", value: counts.deny, className: "bg-deny" },
                {
                  key: "needs_approval",
                  value: counts.needs_approval,
                  className: "bg-approval",
                },
                {
                  key: "allow",
                  value: counts.allow,
                  className: "bg-allow-fill",
                },
              ]}
            />

            {!!anomaliesQ.data?.length && (
              <AnomaliesCard
                rows={anomaliesQ.data}
                onRowClick={(from, to, userId) =>
                  setF((p) => ({
                    ...p,
                    customMode: true,
                    start_date: startOfDay(from),
                    end_date: endOfDay(to),
                    user: userId,
                  }))
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
                    Decisions per {bucketUnit(bucketMinutes)}
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
                {days.length ? (
                  <StackedBars buckets={days} series={OUTCOME_SERIES} />
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

            <EventsTable
              rows={listQ.data?.rows ?? []}
              total={listQ.data?.total ?? 0}
              onSelect={setSel}
              selectedId={sel?.event_id}
              onLoadMore={loadMore}
              loadingMore={listQ.isFetching}
              onExport={exportJsonl}
            />
          </div>
        )}
      </div>

      <DetailDrawer
        event={sel}
        related={related}
        projectId={projectId}
        onClose={() => setSel(null)}
        onSelect={setSel}
        setF={setF}
      />
    </>
  );
}
