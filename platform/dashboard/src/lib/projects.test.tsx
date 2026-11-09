/**
 * useCreateProject — a new project is in its org's cached list as soon as
 * the request returns, so selecting it straight away isn't taken for a
 * deleted project (which the AppShell bootstrap would swap out).
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { makeQueryClient } from "@/test/render";
import { type ProjectRead, useCreateProject } from "./projects";

const project = (id: string): ProjectRead => ({
  id,
  org_id: "org-1",
  name: id,
  created_at: "2026-01-01T00:00:00Z",
});

afterEach(() => vi.restoreAllMocks());

describe("useCreateProject", () => {
  it("adds the new project to the org's cached list at once", async () => {
    vi.spyOn(window, "fetch").mockImplementation(async (_input, init) =>
      init?.method === "POST"
        ? new Response(JSON.stringify(project("p2")), { status: 201 })
        : // The refetch hangs: only the immediate cache update counts.
          new Promise(() => {}),
    );
    const qc = makeQueryClient();
    qc.setQueryData(["projects", "org-1"], [project("p1")]);
    const { result } = renderHook(() => useCreateProject(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      ),
    });
    await act(() => result.current.mutateAsync({ orgId: "org-1", name: "p2" }));
    expect(
      qc.getQueryData<ProjectRead[]>(["projects", "org-1"])?.map((p) => p.id),
    ).toEqual(["p1", "p2"]);
  });
});
