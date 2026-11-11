import { useMemo } from "react";
import { Link, useNavigate, useOutletContext } from "react-router-dom";

import { useProjectScoped } from "@/lib/active";
import {
  type AuditFilters,
  auditUrl,
  EMPTY_AUDIT_FILTERS,
} from "@/lib/audit-filters";
import { rangeScope, useRangeParams } from "@/lib/range-params";
import {
  useAuditStatsQueries,
  useOutcomeStats,
} from "@/components/audit/outcome-stats";
import { DecisionsChartCard, OutcomeStatStrip } from "@/components/audit/stats";
import { BreakdownCard } from "@/components/audit/pieces";
import { displayNoValue } from "@/components/audit/chart-tokens";
import { Card } from "@/components/ui/card";
import { DashboardRangePicker } from "@/components/ui/dashboard-range-picker";
import type { HubContext } from "./AgentHub";

/** The Stats tab — the Audit page's stat strip, chart and breakdown, scoped
 * to this agent. The range lives in the URL (same keys as /audit), so a
 * reload or a shared link keeps it. Drilling in (a tile or a breakdown row)
 * opens /audit with the agent and that slice applied. */
export function AgentStatsTab() {
  const ctx = useOutletContext<HubContext>();
  const agent = ctx.agent.name;
  const { projectId } = useProjectScoped();
  const navigate = useNavigate();
  // 24h by default, matching the Overview's KPIs (/audit opens on 30d).
  const [f, setF] = useRangeParams("24h");

  const scope = { ...rangeScope(f), agent };
  const { summaryQ, tsQ } = useAuditStatsQueries(scope, projectId);
  // Placeholder data is the previous range's: show it as loading rather than
  // under the new range's labels and averages.
  const summary = summaryQ.isPlaceholderData ? undefined : summaryQ.data;
  const roles = useMemo(
    () => summary?.by_role.map(displayNoValue) ?? [],
    [summary],
  );
  const stats = useOutcomeStats(
    summary,
    tsQ.isPlaceholderData ? undefined : tsQ.data,
    f.range,
    f.start_date,
    f.end_date,
  );

  // The audit filters this tab stands for; a drill-down navigates to them.
  const auditF: AuditFilters = { ...EMPTY_AUDIT_FILTERS, ...f, agent };
  const toAudit = (next: AuditFilters) => navigate(auditUrl(next));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-meta text-muted-foreground">
          Policy decisions for{" "}
          <span className="font-mono text-foreground">{agent}</span>, from the
          audit log.
        </span>
        <div className="flex items-center gap-3">
          <Link
            to={auditUrl(auditF)}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            Open in Audit →
          </Link>
          <DashboardRangePicker f={f} setF={setF} />
        </div>
      </div>

      {summaryQ.isError && !summary ? (
        <Card className="grid place-items-center p-10 text-sm text-deny">
          Couldn't load this agent's stats. Refresh to retry.
        </Card>
      ) : !summary ? (
        <Card className="grid place-items-center p-10 text-sm text-muted-foreground">
          Loading stats…
        </Card>
      ) : (
        <>
          <OutcomeStatStrip
            stats={stats}
            onOutcome={(outcome) => toAudit({ ...auditF, outcome })}
          />
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
            <DecisionsChartCard
              stats={stats}
              status={
                tsQ.isError && !tsQ.data
                  ? "error"
                  : !tsQ.data || tsQ.isPlaceholderData
                    ? "loading"
                    : undefined
              }
            />
            <BreakdownCard
              byTool={summary.by_tool}
              byRole={roles}
              dims={["tool", "role"]}
              f={auditF}
              setF={(update) => toAudit(update(auditF))}
            />
          </div>
        </>
      )}
    </div>
  );
}
