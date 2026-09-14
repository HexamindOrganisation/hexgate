/**
 * Hook tests for the compose policy-editor React Query layer: the graph read,
 * the on-demand decision test, the file write mutations (which merge the
 * returned row into the cached list + bust the derived reads), the debounced
 * preview value, and the org-role gate.
 */

import type { ReactNode } from "react";

import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";

import {
  useCanManagePolicy,
  useDebouncedValue,
  useDeleteFile,
  usePolicyGraph,
  usePolicyPreview,
  useRenameFile,
  useResolvedPolicy,
  useTestPolicy,
  useUpsertFile,
} from "./policy_files";
import { useActive } from "./active";

function makeClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

function wrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

/** Route table keyed by "METHOD path" (query string dropped). */
function stubFetch(routes: Record<string, unknown>): void {
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = (typeof input === "string" ? input : input.toString()).split(
        "?",
      )[0];
      const key = `${init?.method ?? "GET"} ${path}`;
      const body = key in routes ? routes[key] : routes[path];
      if (body === undefined) return new Response("not found", { status: 404 });
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  );
}

afterEach(() => vi.restoreAllMocks());

describe("usePolicyGraph", () => {
  it("reads the graph and stays disabled while closed", async () => {
    stubFetch({
      "/v1/projects/p1/policy/graph": {
        nodes: [{ id: "agent:a", kind: "agent", label: "a" }],
        edges: [],
      },
    });

    const closed = renderHook(() => usePolicyGraph("p1", "support", false), {
      wrapper: wrapper(makeClient()),
    });
    expect(closed.result.current.fetchStatus).toBe("idle");

    const { result } = renderHook(() => usePolicyGraph("p1", "support", true), {
      wrapper: wrapper(makeClient()),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.nodes).toHaveLength(1);
  });

  it("is disabled when the project id is null", () => {
    const { result } = renderHook(() => usePolicyGraph(null), {
      wrapper: wrapper(makeClient()),
    });
    expect(result.current.fetchStatus).toBe("idle");
  });
});

describe("useTestPolicy", () => {
  it("posts a decision test and resolves the verdict", async () => {
    stubFetch({
      "POST /v1/projects/p1/policy/test": {
        outcome: "deny",
        reason: "over limit",
        violations: ["args.amount <= 100"],
        hint: null,
      },
    });
    const { result } = renderHook(() => useTestPolicy("p1"), {
      wrapper: wrapper(makeClient()),
    });
    act(() => {
      result.current.mutate({ role: "support", tool: "refund", args: {} });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.outcome).toBe("deny");
  });
});

describe("useUpsertFile", () => {
  it("merges the saved row into the cached file list", async () => {
    const qc = makeClient();
    qc.setQueryData(
      ["policy-files", "p1"],
      [
        {
          name: "policy.yaml",
          content: "old",
          content_hash: "h0",
          updated_at: "t",
        },
      ],
    );
    stubFetch({
      "PUT /v1/projects/p1/policy-files/caps/refunds.yaml": {
        name: "caps/refunds.yaml",
        content: "new",
        content_hash: "h1",
        updated_at: "t1",
      },
    });
    const { result } = renderHook(() => useUpsertFile("p1"), {
      wrapper: wrapper(qc),
    });
    act(() => {
      result.current.mutate({ name: "caps/refunds.yaml", content: "new" });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const cached = qc.getQueryData(["policy-files", "p1"]) as {
      name: string;
    }[];
    expect(cached.map((f) => f.name).sort()).toEqual([
      "caps/refunds.yaml",
      "policy.yaml",
    ]);
  });
});

describe("useDeleteFile", () => {
  it("drops the deleted row from the cached list", async () => {
    const qc = makeClient();
    qc.setQueryData(
      ["policy-files", "p1"],
      [
        {
          name: "policy.yaml",
          content: "a",
          content_hash: "h",
          updated_at: "t",
        },
        { name: "caps.yaml", content: "b", content_hash: "h", updated_at: "t" },
      ],
    );
    vi.spyOn(window, "fetch").mockImplementation(
      async () => new Response(null, { status: 204 }),
    );
    const { result } = renderHook(() => useDeleteFile("p1"), {
      wrapper: wrapper(qc),
    });
    act(() => {
      result.current.mutate("caps.yaml");
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const cached = qc.getQueryData(["policy-files", "p1"]) as {
      name: string;
    }[];
    expect(cached.map((f) => f.name)).toEqual(["policy.yaml"]);
  });
});

describe("useResolvedPolicy placeholderData", () => {
  it("carries roles across an agent switch but not a project switch", async () => {
    const qc = makeClient();
    stubFetch({
      "/v1/projects/pA/policy/resolve": { roles: { support: {}, billing: {} } },
      // pB is a classic project — resolve 422s, so no fresh data arrives.
      "/v1/projects/pB/policy/resolve": undefined,
    });

    const view = renderHook(
      ({ pid, agent }) => useResolvedPolicy(pid, undefined, agent),
      { wrapper: wrapper(qc), initialProps: { pid: "pA", agent: "bot1" } },
    );
    await waitFor(() => expect(view.result.current.isSuccess).toBe(true));
    expect(Object.keys(view.result.current.data ?? {})).toEqual([
      "support",
      "billing",
    ]);

    // Agent switch, same project: the previous roles stay on screen.
    view.rerender({ pid: "pA", agent: "bot2" });
    expect(view.result.current.data).toBeDefined();

    // Project switch to a project that yields no data: old roles must NOT leak.
    view.rerender({ pid: "pB", agent: "bot2" });
    await waitFor(() => expect(view.result.current.data).toBeUndefined());
  });
});

describe("useRenameFile", () => {
  it("writes the new name, rewrites importers, and deletes the old", async () => {
    const qc = makeClient();
    qc.setQueryData(
      ["policy-files", "p1"],
      [
        {
          name: "policy.yaml",
          content: "roles:\n  default: { import: [ caps/refunds.yaml ] }\n",
          content_hash: "h",
          updated_at: "t",
        },
        {
          name: "caps/refunds.yaml",
          content: "tools: {}\n",
          content_hash: "h",
          updated_at: "t",
        },
      ],
    );
    const calls: { method: string; path: string; body?: string }[] = [];
    vi.spyOn(window, "fetch").mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          method: init?.method ?? "GET",
          path: (typeof input === "string" ? input : input.toString()).split(
            "?",
          )[0],
          body: init?.body as string | undefined,
        });
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    );

    const { result } = renderHook(() => useRenameFile("p1"), {
      wrapper: wrapper(qc),
    });
    act(() => {
      result.current.mutate({
        oldName: "caps/refunds.yaml",
        newName: "shared/refunds.yaml",
      });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // 1. write new name, 2. rewrite the importer, 3. delete old — in order.
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
      "PUT /v1/projects/p1/policy-files/shared/refunds.yaml",
      "PUT /v1/projects/p1/policy-files/policy.yaml",
      "DELETE /v1/projects/p1/policy-files/caps/refunds.yaml",
    ]);
    // The importer's rewritten body points at the new path.
    expect(calls[1].body).toContain("shared/refunds.yaml");
    expect(calls[1].body).not.toContain("caps/refunds.yaml");
    expect(result.current.data?.importers).toBe(1);
  });

  it("is a no-op when the name is unchanged (no destructive delete)", async () => {
    const qc = makeClient();
    qc.setQueryData(
      ["policy-files", "p1"],
      [{ name: "a.yaml", content: "x", content_hash: "h", updated_at: "t" }],
    );
    const spy = vi.spyOn(window, "fetch");
    const { result } = renderHook(() => useRenameFile("p1"), {
      wrapper: wrapper(qc),
    });
    act(() => {
      result.current.mutate({ oldName: "a.yaml", newName: "a.yaml" });
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(spy).not.toHaveBeenCalled(); // no PUT, no DELETE
  });

  it("refuses to clobber an existing name", async () => {
    const qc = makeClient();
    qc.setQueryData(
      ["policy-files", "p1"],
      [
        { name: "a.yaml", content: "x", content_hash: "h", updated_at: "t" },
        { name: "b.yaml", content: "y", content_hash: "h", updated_at: "t" },
      ],
    );
    const spy = vi.spyOn(window, "fetch");
    const { result } = renderHook(() => useRenameFile("p1"), {
      wrapper: wrapper(qc),
    });
    act(() => {
      result.current.mutate({ oldName: "a.yaml", newName: "b.yaml" });
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toMatch(/already exists/);
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("usePolicyPreview", () => {
  it("posts the draft overlay when enabled", async () => {
    stubFetch({
      "POST /v1/projects/p1/policy/preview": {
        resolved: { default: { tools: {} } },
        lints: [],
      },
    });
    const { result } = renderHook(
      () =>
        usePolicyPreview(
          "p1",
          { name: "policy.yaml", content: "tools: {}" },
          "*",
          true,
        ),
      { wrapper: wrapper(makeClient()) },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.resolved).toEqual({ default: { tools: {} } });
  });

  it("stays idle while the draft is null", () => {
    const { result } = renderHook(
      () => usePolicyPreview("p1", null, "*", true),
      { wrapper: wrapper(makeClient()) },
    );
    expect(result.current.fetchStatus).toBe("idle");
  });
});

describe("useDebouncedValue", () => {
  it("lags the value by the delay, resetting on each change", () => {
    vi.useFakeTimers();
    try {
      const { result, rerender } = renderHook(
        ({ v }) => useDebouncedValue(v, 300),
        { initialProps: { v: "a" } },
      );
      expect(result.current).toBe("a");
      rerender({ v: "b" });
      expect(result.current).toBe("a"); // not yet elapsed
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(result.current).toBe("b");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("useCanManagePolicy", () => {
  it("is true for an owner of the active org", async () => {
    act(() => {
      useActive.setState({ activeOrgId: "org-a", activeProjectId: null });
    });
    stubFetch({
      "/v1/orgs": [
        {
          id: "org-a",
          slug: "org-a",
          name: "Org A",
          created_at: "2026-01-01T00:00:00Z",
          role: "owner",
        },
      ],
    });
    const { result } = renderHook(() => useCanManagePolicy(), {
      wrapper: wrapper(makeClient()),
    });
    await waitFor(() => expect(result.current).toBe(true));
  });

  it("is false for a plain member", async () => {
    act(() => {
      useActive.setState({ activeOrgId: "org-a", activeProjectId: null });
    });
    stubFetch({
      "/v1/orgs": [
        {
          id: "org-a",
          slug: "org-a",
          name: "Org A",
          created_at: "2026-01-01T00:00:00Z",
          role: "member",
        },
      ],
    });
    const { result } = renderHook(() => useCanManagePolicy(), {
      wrapper: wrapper(makeClient()),
    });
    // Wait for orgs to load, then assert the gate stays closed.
    await waitFor(() => expect(result.current).toBe(false));
  });
});
