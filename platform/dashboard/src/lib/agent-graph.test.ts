import { describe, expect, it } from "vitest";

import type { PolicyGraph } from "@/lib/api";
import { agentReachCounts, agentSubgraph } from "./agent-graph";

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
