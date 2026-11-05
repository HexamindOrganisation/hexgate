/** Derives the audit stat strip and chart data from one summary + timeseries
 * pair — shared by the Audit page and the agent hub's Stats tab. */

import { useMemo } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  api,
  type AuditOutcome,
  type AuditScope,
  type AuditSummary,
  type AuditTimeseriesPoint,
} from "@/lib/api";
import { type Range, rangeDays } from "@/lib/date-range";
import type { ChartBucket } from "@/components/ui/charts";
import type { Counts } from "./charts";

/** The summary + timeseries pair the stats render from, polled every 30s. One
 * definition, so the Audit page and the hub share cache entries per scope. */
export function useAuditStatsQueries(
  scope: AuditScope,
  projectId: string | null,
) {
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
  return { summaryQ, tsQ };
}

const ZERO_COUNTS: Counts = { allow: 0, deny: 0, needs_approval: 0, total: 0 };

export interface OutcomeStats {
  counts: Counts;
  /** Bucket width in minutes; null with fewer than two buckets. */
  bucketMinutes: number | null;
  days: ChartBucket[];
  spark: Record<"total" | AuditOutcome, number[]>;
  denyRate: string;
  allowPct: number;
  apprPct: number;
  avgLabel: string;
}

export function useOutcomeStats(
  summary: AuditSummary | undefined,
  timeseries: AuditTimeseriesPoint[] | undefined,
  range: Range,
  start_date: Date | null,
  end_date: Date | null,
): OutcomeStats {
  const counts: Counts = summary
    ? {
        allow: summary.totals.allow,
        deny: summary.totals.deny,
        needs_approval: summary.totals.needs_approval,
        total: summary.totals.all,
      }
    : ZERO_COUNTS;

  // The server returns every bucket in the window (empty ones as zeros), so
  // the width is the gap between any two neighbours.
  const bucketMinutes = useMemo(() => {
    const d = timeseries;
    if (!d || d.length < 2) return null;
    return Math.round((+new Date(d[1].bucket) - +new Date(d[0].bucket)) / 6e4);
  }, [timeseries]);
  const days: ChartBucket[] = useMemo(
    () =>
      (timeseries ?? []).map((p) => ({
        label: new Date(p.bucket),
        mode: (bucketMinutes ?? 0) >= 1440 ? "day" : "hour",
        values: {
          allow: p.allow,
          deny: p.deny,
          needs_approval: p.needs_approval,
        },
        total: p.allow + p.deny + p.needs_approval,
      })),
    [timeseries, bucketMinutes],
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
  const nDays = rangeDays(range, start_date, end_date);
  const avgLabel =
    !start_date && range === "24h"
      ? `${(counts.total / 24).toFixed(1)}/hr avg`
      : `${(counts.total / nDays).toFixed(0)}/day avg`;

  return {
    counts,
    bucketMinutes,
    days,
    spark,
    denyRate,
    allowPct,
    apprPct,
    avgLabel,
  };
}
