import { useMemo } from "react";
import { Link, useNavigate, useOutletContext } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { rangeScope, useRangeParams } from "@/lib/range-params";
import {
  EMPTY_USAGE_FILTERS,
  type UsageFilters,
  usageUrl,
} from "@/lib/usage-filters";
import { UsageBreakdownCard, UsageStatStrip } from "@/components/usage/pieces";
import { displayNoValue } from "@/components/audit/chart-tokens";
import { Card } from "@/components/ui/card";
import { DashboardRangePicker } from "@/components/ui/dashboard-range-picker";
import type { HubContext } from "./AgentHub";

/** The Usage tab — the Usage page's token strip and model / user breakdown,
 * scoped to this agent. Drilling in (a row, or "Open in Usage") links to
 * /usage with this agent and that slice in the query. Top users is left out:
 * that ranking is project-wide. */
export function AgentUsageTab() {
  const ctx = useOutletContext<HubContext>();
  const agent = ctx.agent.name;
  const { projectId } = useProjectScoped();
  const navigate = useNavigate();
  const [f, setF] = useRangeParams("24h");

  // Same key shape as the Usage page, so a matching filter there is a cache hit.
  const scope = { ...rangeScope(f), agent };
  const summaryQ = useQuery({
    queryKey: ["usage", "summary", projectId, scope],
    enabled: !!projectId,
    queryFn: () => api.getLlmUsageSummary(scope, projectId as string),
    // Best-effort, as on Home: show a failure at once, not after retries.
    retry: false,
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  // Placeholder data is the previous range's: show it as loading rather than
  // under the new range's averages.
  const summary = summaryQ.isPlaceholderData ? undefined : summaryQ.data;
  const users = useMemo(
    () => summary?.by_user.map(displayNoValue) ?? [],
    [summary],
  );

  // The Usage filters this tab stands for; a drill-down opens /usage on them.
  const usageF: UsageFilters = { ...EMPTY_USAGE_FILTERS, ...f, agent };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-meta text-muted-foreground">
          LLM calls and tokens for{" "}
          <span className="font-mono text-foreground">{agent}</span>.
        </span>
        <div className="flex items-center gap-3">
          <Link
            to={usageUrl(usageF)}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            Open in Usage →
          </Link>
          <DashboardRangePicker f={f} setF={setF} />
        </div>
      </div>

      {summaryQ.isError && !summary ? (
        <Card className="grid place-items-center p-10 text-sm text-deny">
          Couldn't load this agent's usage.
        </Card>
      ) : !summary ? (
        <Card className="grid place-items-center p-10 text-sm text-muted-foreground">
          Loading usage…
        </Card>
      ) : summary.totals.calls === 0 ? (
        <Card className="grid place-items-center p-10 text-center text-sm text-muted-foreground">
          No LLM calls recorded for this agent in this range.
        </Card>
      ) : (
        <>
          <UsageStatStrip
            totals={summary.totals}
            range={f.range}
            start_date={f.start_date}
            end_date={f.end_date}
          />
          <UsageBreakdownCard
            byModel={summary.by_model}
            byUser={users}
            dims={["model", "user"]}
            f={usageF}
            setF={(update) => navigate(usageUrl(update(usageF)))}
          />
        </>
      )}
    </div>
  );
}
