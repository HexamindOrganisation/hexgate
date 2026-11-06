/**
 * The audit stat strip and decisions-over-time chart, shared by the Audit page
 * and the agent hub's Stats tab. Their data comes from `useOutcomeStats`.
 */

import type { AuditOutcome } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { StackedBars } from "@/components/ui/charts";
import { StatStrip } from "@/components/ui/stat-strip";
import { cn } from "@/lib/utils";
import { OUT_SWATCH, OUTCOME_SERIES } from "./chart-tokens";
import type { OutcomeStats } from "./outcome-stats";

/** "day", "hour", "6 hours", "15 minutes" for a bucket width. */
function bucketUnit(minutes: number): string {
  if (minutes >= 1440 && minutes % 1440 === 0)
    return minutes === 1440 ? "day" : `${minutes / 1440} days`;
  if (minutes % 60 === 0)
    return minutes === 60 ? "hour" : `${minutes / 60} hours`;
  return minutes === 1 ? "minute" : `${minutes} minutes`;
}

/** Decisions / Denied / Needs approval / Allowed, with sparklines. Tiles are
 * clickable only when `onOutcome` is given. */
export function OutcomeStatStrip({
  stats,
  onOutcome,
  activeOutcome,
}: {
  stats: OutcomeStats;
  onOutcome?: (o: AuditOutcome) => void;
  activeOutcome?: "" | AuditOutcome;
}) {
  const { counts, spark } = stats;
  const click = (o: AuditOutcome) =>
    onOutcome ? () => onOutcome(o) : undefined;
  return (
    <StatStrip
      stats={[
        {
          key: "total",
          label: "Decisions",
          value: counts.total.toLocaleString(),
          sub: stats.avgLabel,
          spark: spark.total,
          sparkColor: "hsl(var(--muted-foreground))",
        },
        {
          key: "deny",
          label: "Denied",
          swatchClass: OUT_SWATCH.deny,
          value: counts.deny.toLocaleString(),
          valueClass: "text-deny",
          sub: `${stats.denyRate}% deny rate`,
          spark: spark.deny,
          sparkColor: "hsl(var(--semantic-deny))",
          onClick: click("deny"),
          active: activeOutcome === "deny",
        },
        {
          key: "needs_approval",
          label: "Needs approval",
          swatchClass: OUT_SWATCH.needs_approval,
          value: counts.needs_approval.toLocaleString(),
          valueClass: "text-approval",
          sub: `${stats.apprPct}% of decisions`,
          spark: spark.needs_approval,
          sparkColor: "hsl(var(--semantic-approval))",
          onClick: click("needs_approval"),
          active: activeOutcome === "needs_approval",
        },
        {
          key: "allow",
          label: "Allowed",
          swatchClass: OUT_SWATCH.allow,
          value: counts.allow.toLocaleString(),
          sub: `${stats.allowPct}% of decisions`,
          spark: spark.allow,
          sparkColor: "hsl(var(--semantic-allow))",
          onClick: click("allow"),
          active: activeOutcome === "allow",
        },
      ]}
      split={[
        { key: "deny", value: counts.deny, className: OUT_SWATCH.deny },
        {
          key: "needs_approval",
          value: counts.needs_approval,
          className: OUT_SWATCH.needs_approval,
        },
        {
          key: "allow",
          value: counts.allow,
          className: OUT_SWATCH.allow,
        },
      ]}
    />
  );
}

/** The stacked decisions-over-time chart, with its own empty state. `status`
 * replaces the chart while the timeseries is loading or failed, so a missing
 * series never reads as "No decisions". */
export function DecisionsChartCard({
  stats,
  status,
}: {
  stats: OutcomeStats;
  status?: "loading" | "error";
}) {
  const { bucketMinutes, days } = stats;
  return (
    <Card className="p-5">
      <div className="mb-5 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-body font-semibold">
          {bucketMinutes === null
            ? "Decisions over time"
            : `Decisions per ${bucketUnit(bucketMinutes)}`}
          {/* Server day buckets are UTC days; say so, since the
              decision log groups by local day. */}
          {bucketMinutes !== null && bucketMinutes >= 1440 && (
            <span className="ml-1.5 text-meta font-normal text-muted-foreground">
              UTC
            </span>
          )}
        </h2>
        <div className="flex gap-3.5 text-xs text-muted-foreground">
          {[...OUTCOME_SERIES].reverse().map((s) => (
            <span key={s.key} className="flex items-center gap-1.5">
              <span className={`size-2 rounded-sm ${s.swatchClass}`} />
              {s.label}
            </span>
          ))}
        </div>
      </div>
      {status ? (
        <div
          className={cn(
            "flex h-52 items-center justify-center text-meta",
            status === "error" ? "text-deny" : "text-muted-foreground",
          )}
        >
          {status === "error"
            ? "Couldn't load the chart. Refresh to retry."
            : "Loading chart…"}
        </div>
      ) : days.some((d) => d.total > 0) ? (
        <StackedBars buckets={days} series={OUTCOME_SERIES} />
      ) : (
        <div className="flex h-52 items-center justify-center text-meta text-muted-foreground">
          No decisions in this range.
        </div>
      )}
    </Card>
  );
}
