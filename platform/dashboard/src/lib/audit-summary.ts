import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, type AuditBreakdownRow } from "@/lib/api";

/** The project's last-24h audit summary — one cache entry for every view
 * that shows 24h numbers (Home, Agents), polled every 30s. */
export function useAuditSummary24h(projectId: string | null) {
  return useQuery({
    queryKey: ["audit", "summary", projectId, { window: "24h" }],
    enabled: !!projectId,
    queryFn: () => api.getAuditSummary({ window: "24h" }, projectId!),
    refetchInterval: 30_000,
  });
}

type Activity = Pick<AuditBreakdownRow, "all" | "deny" | "needs_approval">;
const IDLE: Activity = { all: 0, deny: 0, needs_approval: 0 };

/** The 24h summary plus `activityOf(agent)`: null until the summary lands
 * (unknown, not idle), zeros for an agent it doesn't list (no calls). One
 * rule for every view that shows an agent's 24h counts. */
export function useAgentActivity24h(projectId: string | null) {
  const summary = useAuditSummary24h(projectId);
  const byAgent = useMemo(
    () => new Map(summary.data?.by_agent.map((r) => [r.key, r])),
    [summary.data],
  );
  const loaded = !!summary.data;
  const activityOf = useCallback(
    (name: string): Activity | null =>
      loaded ? (byAgent.get(name) ?? IDLE) : null,
    [loaded, byAgent],
  );
  return { summary, activityOf };
}

/** The share of `all` calls denied, as "12.5", or null with no calls: "—"
 * reads apart from a real 0.0%. */
export const denyRatePct = (deny: number, all: number) =>
  all > 0 ? ((deny / all) * 100).toFixed(1) : null;
