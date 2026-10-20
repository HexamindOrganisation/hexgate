import type { AuditAnomaly } from "@/lib/api";

/** Worst first: high severity, then deny rate. The API orders by user, so
 * every view that shows only the top few must rank with this. */
export function rankAnomalies(rows: AuditAnomaly[]): AuditAnomaly[] {
  return [...rows].sort(
    (a, b) =>
      Number(b.severity === "high") - Number(a.severity === "high") ||
      b.deny_rate - a.deny_rate,
  );
}
