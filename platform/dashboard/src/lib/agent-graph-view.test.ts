import { describe, expect, it } from "vitest";

import type { PolicyGraph, PolicyGraphEdge } from "./api";
import {
  compactAgentGraph,
  roleOptionsFor,
  rolesThatRun,
} from "./agent-graph-view";

const edge = (
  source: string,
  target: string,
  kind: PolicyGraphEdge["kind"],
  over: Partial<PolicyGraphEdge> = {},
): PolicyGraphEdge => ({
  source,
  target,
  kind,
  via: kind === "reach" ? "tool" : null,
  verdict: "allow",
  constraints: [],
  roles: [],
  ...over,
});

/** Agent a with n tools (the last `denied` of them denied). */
function agentWith(n: number, denied = 0): PolicyGraph {
  const tools = Array.from({ length: n }, (_, i) => `tool:t${i}`);
  return {
    roles: [],
    nodes: [
      { id: "agent:a", kind: "agent", label: "a" },
      ...tools.map((id) => ({ id, kind: "tool" as const, label: id.slice(5) })),
    ],
    edges: tools.map((t, i) =>
      edge("agent:a", t, "call", {
        verdict: i >= n - denied ? "deny" : "allow",
      }),
    ),
  };
}

/** Agent a run by `n` roles (the first `denied` of them denied). */
function runBy(n: number, denied = 0): PolicyGraph {
  const roles = Array.from({ length: n }, (_, i) => `role:r${i}`);
  return {
    roles: [],
    nodes: [
      { id: "agent:a", kind: "agent", label: "a" },
      ...roles.map((id) => ({ id, kind: "role" as const, label: id })),
    ],
    edges: roles.map((r, i) =>
      edge(r, "agent:a", "admission", {
        verdict: i < denied ? "deny" : "allow",
      }),
    ),
  };
}

describe("compactAgentGraph", () => {
  it("draws a few tools as they are", () => {
    const v = compactAgentGraph(agentWith(8));
    expect(v.graph.nodes).toHaveLength(9);
    expect(v.tools.size).toBe(0);
  });

  it("takes tools off the canvas by verdict past eight", () => {
    const v = compactAgentGraph(agentWith(10, 3));
    // Only real nodes and edges are drawn: the agent alone here.
    expect(v.graph.nodes.map((n) => n.id)).toEqual(["agent:a"]);
    expect(v.graph.edges).toHaveLength(0);
    expect(v.tools.get("allow")).toHaveLength(7);
    expect(v.tools.get("deny")).toHaveLength(3);
  });

  it("leaves roles past five off the canvas, denied ones first", () => {
    const v = compactAgentGraph(runBy(7, 2));
    expect(v.hiddenRoles).toBe(2);
    expect(v.graph.nodes.some((n) => n.id === "role:r0")).toBe(false);
    expect(v.graph.nodes.some((n) => n.id === "role:r6")).toBe(true);
    // Every drawn edge joins drawn nodes.
    const ids = new Set(v.graph.nodes.map((n) => n.id));
    expect(
      v.graph.edges.every((e) => ids.has(e.source) && ids.has(e.target)),
    ).toBe(true);
  });

  it("counts a sub-agent reached two ways as one", () => {
    const subs = Array.from({ length: 5 }, (_, i) => `agent:s${i}`);
    const g: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:a", kind: "agent", label: "a" },
        ...subs.map((id) => ({ id, kind: "agent" as const, label: id })),
      ],
      edges: [
        ...subs.map((s) => edge("agent:a", s, "reach")),
        edge("agent:a", "agent:s0", "reach", { via: "handoff" }),
      ],
    };
    const v = compactAgentGraph(g);
    expect(v.hiddenAgents).toBe(0);
    expect(v.graph.edges).toHaveLength(6);
  });

  it("leaves sub-agents past five off the canvas", () => {
    const subs = Array.from({ length: 7 }, (_, i) => `agent:s${i}`);
    const g: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:a", kind: "agent", label: "a" },
        ...subs.map((id) => ({ id, kind: "agent" as const, label: id })),
      ],
      edges: subs.map((s) => edge("agent:a", s, "reach")),
    };
    expect(compactAgentGraph(g).hiddenAgents).toBe(2);
  });
});

describe("roleOptionsFor", () => {
  it("says which roles run the agent", () => {
    expect(roleOptionsFor(["r0", "r1"], new Set(["r1"]))).toEqual([
      { name: "r0", runs: false },
      { name: "r1", runs: true },
    ]);
  });

  it("leaves 'runs' unknown when no role gates running it", () => {
    expect(roleOptionsFor(["ops"], null)[0].runs).toBeNull();
  });
});

describe("rolesThatRun (gating)", () => {
  it("lets no role run an agent with no admission once any agent has one", () => {
    const g = runBy(1);
    g.nodes.push({ id: "agent:b", kind: "agent", label: "b" });
    g.declared_agents = ["a", "b"];
    expect(rolesThatRun(g, ["b"]).get("b")?.size).toBe(0);
    expect([...(rolesThatRun(g, ["a"]).get("a") ?? [])]).toEqual(["r0"]);
  });

  it("is null for every agent when nothing gates running", () => {
    expect(rolesThatRun(agentWith(1), ["a"]).get("a")).toBeNull();
  });
});
