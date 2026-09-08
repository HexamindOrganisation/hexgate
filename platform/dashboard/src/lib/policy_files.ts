/**
 * Compose policy-editor hooks. React Query reads + mutations over the
 * `api.*PolicyFile*` / `api.resolvePolicy` / `api.policyGraph` / `api.testPolicy`
 * calls (all through the shared `request()` helper, so a 401 bounces to
 * /sign-in and error detail is extracted centrally).
 *
 * The file store is the source of truth; the resolved policy, graph, and lints
 * are derived. Every write invalidates the derived reads so the inspector
 * reconciles to stored state after a Save. The live preview (`usePolicyPreview`)
 * is a separate, debounced read of an unsaved draft — it never writes.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useActive } from "./active";
import { api, type PolicyFileDraft } from "./api";
import { useOrgs } from "./orgs";

// Re-export the wire types so component imports (`@/lib/policy_files`) resolve
// without also reaching into `@/lib/api`.
export type { PolicyFileRead, PolicyLint, ResolvedPolicy } from "./api";

const filesKey = (pid: string) => ["policy-files", pid] as const;
const resolveKey = (pid: string, role: string | null, agent: string | null) =>
  ["policy-resolve", pid, role, agent] as const;
const checkKey = (pid: string) => ["policy-check", pid] as const;

/** Bust every derived read for a project after a write. The stored files are
 * authoritative; resolve/graph/check/preview all recompute from them. */
function invalidateDerived(qc: ReturnType<typeof useQueryClient>, pid: string) {
  qc.invalidateQueries({ queryKey: ["policy-resolve", pid] });
  qc.invalidateQueries({ queryKey: ["policy-graph", pid] });
  qc.invalidateQueries({ queryKey: checkKey(pid) });
  qc.invalidateQueries({ queryKey: ["policy-preview", pid] });
}

/** Every compose file in the project. Disabled while `projectId` is null so
 * pages can pass the resolved scope id directly. */
export function usePolicyFiles(projectId: string | null) {
  return useQuery({
    queryKey: filesKey(projectId as string),
    queryFn: () => api.listPolicyFiles(projectId as string),
    enabled: !!projectId,
    staleTime: 30_000,
  });
}

/** The composed effective policy, per role (all roles, or just `role`), for one
 * executing agent (`agent`, default the generic `"*"` column). */
export function useResolvedPolicy(
  projectId: string | null,
  role?: string,
  agent?: string,
  enabled = true,
) {
  return useQuery({
    queryKey: resolveKey(projectId as string, role ?? null, agent ?? null),
    queryFn: () => api.resolvePolicy(projectId as string, role, agent),
    // Skip on classic (non-compose) projects — there's no composed policy to
    // resolve, so the request is wasted and 422s.
    enabled: !!projectId && enabled,
    // 422 when the files don't compose — surfaced via `usePolicyCheck`; don't
    // hammer the endpoint retrying an unresolvable set.
    retry: false,
    staleTime: 15_000,
  });
}

/** Lints over the composed project (diagnostics-as-data, always 200). */
export function usePolicyCheck(projectId: string | null, enabled = true) {
  return useQuery({
    queryKey: checkKey(projectId as string),
    queryFn: () => api.checkPolicy(projectId as string),
    enabled: !!projectId && enabled,
    staleTime: 15_000,
  });
}

/** The resolved policy as a node/edge graph (agents, tools, reach/admission),
 * for one role or the union across roles. */
export function usePolicyGraph(
  projectId: string | null,
  role?: string,
  enabled = true,
) {
  return useQuery({
    queryKey: ["policy-graph", projectId as string, role ?? null],
    queryFn: () => api.policyGraph(projectId as string, role),
    enabled: !!projectId && enabled,
    retry: false,
    staleTime: 15_000,
  });
}

export function useUpsertFile(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string; content: string }) =>
      api.upsertPolicyFile(projectId, input.name, input.content),
    onSuccess: (row) => {
      // Merge the returned row into the cached list (upsert by name) so the
      // tree/tab reflect the new hash without a refetch round-trip.
      qc.setQueryData(
        filesKey(projectId),
        (prev: (typeof row)[] | undefined) => {
          const list = prev ?? [];
          const rest = list.filter((f) => f.name !== row.name);
          return [...rest, row];
        },
      );
      invalidateDerived(qc, projectId);
    },
  });
}

export function useDeleteFile(projectId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.deletePolicyFile(projectId, name),
    onSuccess: (_void, name) => {
      qc.setQueryData(
        filesKey(projectId),
        (prev: { name: string }[] | undefined) =>
          (prev ?? []).filter((f) => f.name !== name),
      );
      invalidateDerived(qc, projectId);
    },
  });
}

/** Evaluate one tool call against the whole resolved policy. A mutation, not a
 * query: it's fired on demand by the tester's Check button. */
export function useTestPolicy(projectId: string) {
  return useMutation({
    mutationFn: (body: Parameters<typeof api.testPolicy>[1]) =>
      api.testPolicy(projectId, body),
  });
}

/**
 * Debounced live preview: resolve + lint the project with `draft` overlaid,
 * without writing. Keyed on the (already debounced) draft so a new keystroke
 * supersedes an in-flight request; `enabled` lets the caller skip the round
 * trip while the draft doesn't parse client-side. `placeholderData` keeps the
 * previous result on screen during a refetch so lints don't flicker.
 */
export function usePolicyPreview(
  projectId: string | null,
  draft: PolicyFileDraft | null,
  agent: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["policy-preview", projectId, JSON.stringify(draft), agent],
    queryFn: () => api.previewPolicy(projectId as string, draft!, agent),
    enabled: !!projectId && enabled && draft !== null,
    retry: false,
    placeholderData: (prev) => prev,
    staleTime: 5_000,
  });
}

/**
 * Value that lags `value` by `ms`, resetting the timer on every change. Only
 * the debounced value should be a query dependency, so the expensive preview
 * round-trip fires after the user pauses, not per keystroke.
 */
export function useDebouncedValue<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return debounced;
}

/** Whether the caller may edit policy in the active org — the file write
 * endpoints are `require_project_admin` server-side, so this gates the Save /
 * tree-edit affordances. Mirrors `useCanManageBans`; false while orgs load. */
export function useCanManagePolicy(): boolean {
  const activeOrgId = useActive((s) => s.activeOrgId);
  const orgsQuery = useOrgs();
  const org = orgsQuery.data?.find((o) => o.id === activeOrgId) ?? null;
  return org?.role === "owner" || org?.role === "admin";
}
