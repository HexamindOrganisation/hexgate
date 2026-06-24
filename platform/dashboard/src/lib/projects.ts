/**
 * Project-domain hooks. React Query reads + mutations for the
 * /v1/orgs/{org_id}/projects and /v1/projects/{id} surface.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "./api";

/** Mirror of platform/api/schemas.py:ProjectRead. */
export interface ProjectRead {
  id: string;
  org_id: string;
  name: string;
  created_at: string;
}

function projectsKey(orgId: string | null) {
  return ["projects", orgId] as const;
}

async function fetchProjects(orgId: string): Promise<ProjectRead[]> {
  const res = await fetch(`/v1/orgs/${orgId}/projects`, {
    credentials: "include",
  });
  if (!res.ok) {
    throw new ApiError(res.status, null, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as ProjectRead[];
}

/** Projects inside an org. ``enabled: !!orgId`` means callers can
 * pass ``activeOrgId`` directly without an extra null-check; the
 * query skips while the active-org isn't resolved yet. */
export function useProjects(orgId: string | null) {
  return useQuery({
    queryKey: projectsKey(orgId),
    queryFn: () => fetchProjects(orgId as string),
    enabled: !!orgId,
    staleTime: 60_000,
  });
}

interface CreateProjectInput {
  orgId: string;
  name: string;
}

async function createProjectRequest(
  input: CreateProjectInput,
): Promise<ProjectRead> {
  const res = await fetch(`/v1/orgs/${input.orgId}/projects`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: input.name }),
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = null;
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as ProjectRead;
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: createProjectRequest,
    onSuccess: (project) => {
      // Bust the listing for the org this project landed in. Use the
      // returned project's org_id rather than the input so a future
      // server-side org-redirect (we don't do this today) still works.
      qc.invalidateQueries({ queryKey: projectsKey(project.org_id) });
    },
  });
}

interface RenameProjectInput {
  projectId: string;
  name: string;
}

async function renameProjectRequest(
  input: RenameProjectInput,
): Promise<ProjectRead> {
  const res = await fetch(`/v1/projects/${input.projectId}`, {
    method: "PATCH",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: input.name }),
  });
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = await res.json();
    } catch {
      detail = null;
    }
    throw new ApiError(res.status, detail, `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as ProjectRead;
}

export function useRenameProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: renameProjectRequest,
    onSuccess: (project) => {
      qc.invalidateQueries({ queryKey: projectsKey(project.org_id) });
    },
  });
}
