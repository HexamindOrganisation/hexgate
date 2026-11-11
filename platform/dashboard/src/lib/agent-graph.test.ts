import { describe, expect, it } from "vitest";

import type { PolicyGraph } from "@/lib/api";
import { agentReachCounts, agentReachMap, agentSubgraph } from "./agent-graph";

const edge = (
  source: string,
  target: string,
  kind: "admission" | "call" | "reach",
): PolicyGraph["edges"][number] => ({
  source,
  target,
  kind,
  via: null,
  verdict: "allow",
  constraints: [],
  roles: [],
});

// a: the subject. a calls t and reaches b (which calls t2). s is a supervisor
// that reaches INTO a (and also calls secret). x is unrelated.
const G: PolicyGraph = {
  roles: ["support"],
  nodes: [
    { id: "agent:a", kind: "agent", label: "a" },
    { id: "agent:b", kind: "agent", label: "b" },
    { id: "agent:s", kind: "agent", label: "s" },
    { id: "agent:x", kind: "agent", label: "x" },
    { id: "tool:t", kind: "tool", label: "t" },
    { id: "tool:t2", kind: "tool", label: "t2" },
    { id: "tool:secret", kind: "tool", label: "secret" },
    { id: "role:support", kind: "role", label: "support" },
  ],
  edges: [
    edge("role:support", "agent:a", "admission"),
    edge("agent:a", "tool:t", "call"),
    edge("agent:a", "agent:b", "reach"),
    edge("agent:b", "tool:t2", "call"),
    edge("agent:s", "agent:a", "reach"),
    edge("agent:s", "tool:secret", "call"),
    edge("agent:x", "tool:t", "call"),
  ],
};

describe("agentSubgraph", () => {
  it("expands the tools the agent's own reach targets can touch", () => {
    const sub = agentSubgraph(G, "a");
    // a's neighbourhood + t2 (via a→b→t2); s is shown as an inbound caller.
    expect(sub.nodes.map((n) => n.id).sort()).toEqual([
      "agent:a",
      "agent:b",
      "agent:s",
      "role:support",
      "tool:t",
      "tool:t2",
    ]);
    expect(sub.edges).toHaveLength(5);
  });

  it("does not pull an inbound caller's own tools into the subgraph", () => {
    const sub = agentSubgraph(G, "a");
    // s reaches INTO a, so a cannot reach s's tool — it must be excluded.
    expect(sub.nodes.some((n) => n.id === "tool:secret")).toBe(false);
    expect(sub.nodes.some((n) => n.id === "agent:x")).toBe(false);
  });

  it("returns an empty graph when the agent isn't present", () => {
    expect(agentSubgraph(G, "ghost").nodes).toHaveLength(0);
  });

  it("stands an unnamed agent in for the generic * agent", () => {
    const g: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:*", kind: "agent", label: "*" },
        { id: "tool:t", kind: "tool", label: "t" },
      ],
      edges: [edge("agent:*", "tool:t", "call")],
    };
    const sub = agentSubgraph(g, "support_bot");
    expect(sub.nodes.find((n) => n.id === "agent:*")?.label).toBe(
      "support_bot",
    );
    expect(agentReachCounts(sub)).toEqual({
      tools: 1,
      mcpTools: 0,
      agents: 0,
    });
  });

  it("neither counts nor follows a denied edge", () => {
    const deny = (e: PolicyGraph["edges"][number]) => ({
      ...e,
      verdict: "deny" as const,
    });
    const g: PolicyGraph = {
      ...G,
      edges: [
        deny(edge("agent:a", "agent:b", "reach")),
        deny(edge("agent:a", "tool:t", "call")),
        edge("agent:b", "tool:t2", "call"),
      ],
    };
    const sub = agentSubgraph(g, "a");
    // b is shown (the denied edge is), but its tool isn't pulled in.
    expect(sub.nodes.some((n) => n.id === "tool:t2")).toBe(false);
    expect(agentReachCounts(sub)).toEqual({
      tools: 0,
      mcpTools: 0,
      agents: 0,
    });
  });

  it("follows permitted reach hop by hop", () => {
    const g: PolicyGraph = {
      ...G,
      nodes: [
        ...G.nodes,
        { id: "tool:t3", kind: "tool", label: "t3" },
        { id: "agent:c", kind: "agent", label: "c" },
      ],
      edges: [
        ...G.edges,
        edge("agent:b", "agent:c", "reach"),
        edge("agent:c", "tool:t3", "call"),
      ],
    };
    expect(agentSubgraph(g, "a").nodes.some((n) => n.id === "tool:t3")).toBe(
      true,
    );
  });

  it("doesn't count a reach to itself as reaching an agent", () => {
    const g: PolicyGraph = {
      ...G,
      edges: [...G.edges, edge("agent:a", "agent:a", "reach")],
    };
    expect(agentReachCounts(agentSubgraph(g, "a")).agents).toBe(1);
  });

  it("speaks for an agent named only as a reach target through *", () => {
    // supervisor hands off to support_bot, which the policy doesn't declare:
    // it resolves like * and may call refund.
    const g: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:*", kind: "agent", label: "*" },
        { id: "agent:supervisor", kind: "agent", label: "supervisor" },
        { id: "agent:support_bot", kind: "agent", label: "support_bot" },
        { id: "tool:refund", kind: "tool", label: "refund" },
      ],
      edges: [
        edge("agent:supervisor", "agent:support_bot", "reach"),
        edge("agent:*", "tool:refund", "call"),
      ],
    };
    const sub = agentSubgraph(g, "support_bot");
    expect(agentReachCounts(sub).tools).toBe(1);
    expect(sub.nodes.some((n) => n.id === "agent:supervisor")).toBe(true);
  });

  it("keeps a declared agent with only roles admitting it as itself", () => {
    const g: PolicyGraph = {
      roles: ["support"],
      nodes: [
        { id: "agent:*", kind: "agent", label: "*" },
        { id: "agent:bot", kind: "agent", label: "bot" },
        { id: "role:support", kind: "role", label: "support" },
        { id: "tool:refund", kind: "tool", label: "refund" },
      ],
      edges: [
        edge("role:support", "agent:bot", "admission"),
        edge("agent:*", "tool:refund", "call"),
      ],
    };
    // Not *'s grants: bot is declared, with none of its own.
    expect(agentReachCounts(agentSubgraph(g, "bot")).tools).toBe(0);
  });
});

const NONE = { reaches: [], calledBy: [] };
const namesOf = (g: PolicyGraph) =>
  g.nodes
    .filter((n) => n.kind === "agent" && n.label !== "*")
    .map((n) => n.label);
const agentReach = (g: PolicyGraph, name: string) =>
  agentReachMap(g, namesOf(g)).get(name) ?? NONE;

const agent = (n: string) => ({
  id: `agent:${n}`,
  kind: "agent" as const,
  label: n,
});
const reach = (s: string, t: string, verdict: "allow" | "deny" = "allow") => ({
  source: `agent:${s}`,
  target: `agent:${t}`,
  kind: "reach" as const,
  via: "tool" as const,
  verdict,
  constraints: [],
  roles: [],
});

describe("agentReachMap", () => {
  it("separates outbound reach from inbound callers", () => {
    // a reaches b and is reached by s.
    expect(agentReach(G, "a")).toEqual({ reaches: ["b"], calledBy: ["s"] });
    expect(agentReach(G, "b")).toEqual({ reaches: [], calledBy: ["a"] });
    expect(agentReach(G, "s")).toEqual({ reaches: ["a"], calledBy: [] });
  });

  it("ignores tool edges — only agent↔agent counts as delegation", () => {
    // x only calls a tool; it neither reaches nor is reached by an agent.
    expect(agentReach(G, "x")).toEqual(NONE);
  });

  it("is empty for an agent not in the graph", () => {
    expect(agentReach(G, "ghost")).toEqual(NONE);
  });

  it("drops a denied reach and a reach to itself", () => {
    const g: PolicyGraph = {
      roles: [],
      nodes: [agent("support"), agent("admin"), agent("ops")],
      edges: [reach("support", "admin", "deny"), reach("ops", "ops")],
    };
    expect(agentReachMap(g, namesOf(g)).size).toBe(0);
  });

  it("gives an agent the policy doesn't declare the generic * agent's reach", () => {
    const g: PolicyGraph = {
      roles: [],
      nodes: [agent("support"), agent("ops"), agent("*")],
      edges: [reach("*", "ops")],
    };
    expect(agentReach(g, "support")).toEqual({
      reaches: ["ops"],
      calledBy: [],
    });
    // ops resolves through * too, but reaching itself is no delegation.
    expect(agentReach(g, "ops")).toEqual({
      reaches: [],
      calledBy: ["support"],
    });
    // * itself is no one.
    expect(agentReachMap(g, namesOf(g)).has("*")).toBe(false);
  });

  it("reads a reach to * as any agent, a caller of every other", () => {
    const g: PolicyGraph = {
      roles: [],
      nodes: [agent("support"), agent("admin"), agent("*")],
      edges: [reach("support", "*"), reach("admin", "admin", "deny")],
    };
    expect(agentReach(g, "support")).toEqual({
      reaches: ["any agent"],
      calledBy: [],
    });
    expect(agentReach(g, "admin")).toEqual({
      reaches: [],
      calledBy: ["support"],
    });
  });

  it("doesn't count a call edge as delegation", () => {
    const g: PolicyGraph = {
      roles: [],
      nodes: [agent("a"), agent("b")],
      edges: [{ ...reach("a", "b"), kind: "call" as const, via: null }],
    };
    expect(agentReachMap(g, namesOf(g)).size).toBe(0);
  });
});
