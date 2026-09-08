/**
 * Request-shape tests for the compose policy-file API surface: right verb,
 * right URL (nested file names stay un-encoded so `{name:path}` keeps the
 * slashes), right body, and the `.roles` unwrapping on resolve.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

interface Captured {
  url: string;
  method: string;
  body: unknown;
}

function capture(responseBody: unknown, status = 200): Captured[] {
  const calls: Captured[] = [];
  vi.spyOn(window, "fetch").mockImplementation(async (input, init) => {
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    });
    if (status === 204) return new Response(null, { status });
    return new Response(JSON.stringify(responseBody), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  return calls;
}

afterEach(() => vi.restoreAllMocks());

const P = "p1";

describe("policy-file api requests", () => {
  it("lists files", async () => {
    const calls = capture([]);
    await api.listPolicyFiles(P);
    expect(calls[0]).toMatchObject({
      url: "/v1/projects/p1/policy-files",
      method: "GET",
    });
  });

  it("upserts a nested file, keeping the slash separators", async () => {
    const calls = capture({});
    await api.upsertPolicyFile(P, "caps/refunds.yaml", "yaml");
    expect(calls[0]).toMatchObject({
      url: "/v1/projects/p1/policy-files/caps/refunds.yaml",
      method: "PUT",
      body: { content: "yaml" },
    });
  });

  it("percent-encodes segments but keeps the slashes", async () => {
    const calls = capture({});
    await api.upsertPolicyFile(P, "team a/pol #1.yaml", "y");
    expect(calls[0].url).toBe(
      "/v1/projects/p1/policy-files/team%20a/pol%20%231.yaml",
    );
  });

  it("deletes a file (204)", async () => {
    const calls = capture(null, 204);
    await api.deletePolicyFile(P, "policy.yaml");
    expect(calls[0]).toMatchObject({
      url: "/v1/projects/p1/policy-files/policy.yaml",
      method: "DELETE",
    });
  });

  it("previews a draft with {name, content, agent}", async () => {
    const calls = capture({ resolved: null, lints: [] });
    await api.previewPolicy(P, { name: "policy.yaml", content: "x" }, "bot");
    expect(calls[0]).toMatchObject({
      url: "/v1/projects/p1/policy/preview",
      method: "POST",
      body: { name: "policy.yaml", content: "x", agent: "bot" },
    });
  });

  it("preview defaults the agent to *", async () => {
    const calls = capture({ resolved: null, lints: [] });
    await api.previewPolicy(P, { name: "policy.yaml", content: "x" });
    expect(calls[0].body).toMatchObject({ agent: "*" });
  });

  it("resolves and unwraps .roles", async () => {
    const calls = capture({ roles: { default: { tools: {} } } });
    const roles = await api.resolvePolicy(P, undefined, "bot");
    expect(calls[0].url).toBe("/v1/projects/p1/policy/resolve?agent=bot");
    expect(roles).toEqual({ default: { tools: {} } });
  });

  it("fetches the graph with a role filter", async () => {
    const calls = capture({ nodes: [], edges: [] });
    await api.policyGraph(P, "support");
    expect(calls[0]).toMatchObject({
      url: "/v1/projects/p1/policy/graph?role=support",
      method: "GET",
    });
  });

  it("posts a decision test", async () => {
    const calls = capture({ outcome: "allow", violations: [] });
    await api.testPolicy(P, { role: "default", tool: "a", args: {} });
    expect(calls[0]).toMatchObject({
      url: "/v1/projects/p1/policy/test",
      method: "POST",
      body: { role: "default", tool: "a" },
    });
  });
});
