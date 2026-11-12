import { endOfDay, startOfDay } from "date-fns";

import type { AuditAnomaly } from "@/lib/api";
import type { AuditFilters } from "@/lib/audit-filters";

import { NO_VALUE_LABEL } from "./chart-tokens";

/** Worst first: high severity, then deny rate. The API orders by user, so
 * every view that shows only the top few must rank with this. */
export function rankAnomalies(rows: AuditAnomaly[]): AuditAnomaly[] {
  return [...rows].sort(
    (a, b) =>
      Number(b.severity === "high") - Number(a.severity === "high") ||
      b.deny_rate - a.deny_rate,
  );
}

/** Audit filters that narrow the page to one burst: its user (the "(none)"
 * bucket for callers with no id) over the days it spans. */
export function burstFilters(
  userId: string,
  from: Date,
  to: Date,
): Pick<AuditFilters, "customMode" | "start_date" | "end_date" | "user"> {
  return {
    customMode: true,
    start_date: startOfDay(from),
    end_date: endOfDay(to),
    user: userId === "" ? NO_VALUE_LABEL : userId,
  };
}
