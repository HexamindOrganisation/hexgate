import { describe, expect, it } from "vitest";

import type { PolicyGraph } from "@/lib/api";
import { agentSubgraph } from "./agent-graph";

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
});
