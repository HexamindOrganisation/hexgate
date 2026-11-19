/** The project's agents with their manifests: one query, shared by the
 * sidebar, /agents, the hub, Home and the Policies graph picker. */

import { useQuery } from "@tanstack/react-query";

import { api } from "./api";

export function useAgentManifests(
  projectId: string | null | undefined,
  {
    enabled = true,
    refetchInterval,
  }: {
    enabled?: boolean;
    refetchInterval?: number;
  } = {},
) {
  return useQuery({
    queryKey: ["agent-manifests", projectId],
    queryFn: () => api.listAgentManifests(projectId as string),
    enabled: !!projectId && enabled,
    refetchInterval,
  });
}
