import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** The project's last-24h audit summary — one cache entry for every view
 * that shows 24h numbers (Home, Agents). Polls only where asked. */
export function useAuditSummary24h(
  projectId: string | null,
  { poll = false }: { poll?: boolean } = {},
) {
  return useQuery({
    queryKey: ["audit", "summary", projectId, { window: "24h" }],
    enabled: !!projectId,
    queryFn: () => api.getAuditSummary({ window: "24h" }, projectId!),
    refetchInterval: poll ? 30_000 : false,
  });
}
