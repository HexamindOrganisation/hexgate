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

const G: PolicyGraph = {
  roles: ["support"],
  nodes: [
    { id: "agent:a", kind: "agent", label: "a" },
    { id: "agent:b", kind: "agent", label: "b" },
    { id: "tool:t", kind: "tool", label: "t" },
    { id: "tool:t2", kind: "tool", label: "t2" },
    { id: "role:support", kind: "role", label: "support" },
    { id: "agent:unrelated", kind: "agent", label: "unrelated" },
  ],
  edges: [
    edge("role:support", "agent:a", "admission"),
    edge("agent:a", "tool:t", "call"),
    edge("agent:a", "agent:b", "reach"),
    edge("agent:b", "tool:t2", "call"),
    edge("agent:unrelated", "tool:t", "call"),
  ],
};

describe("agentSubgraph", () => {
  it("keeps the agent's neighbours and the tools its sub-agents reach", () => {
    const sub = agentSubgraph(G, "a");
    expect(sub.nodes.map((n) => n.id).sort()).toEqual([
      "agent:a",
      "agent:b",
      "role:support",
      "tool:t",
      "tool:t2",
    ]);
    // b is reached by a, so b→t2 is kept — b isn't a dead-end leaf.
    expect(sub.edges).toHaveLength(4);
    // The unrelated agent (not reached by a) is excluded.
    expect(sub.nodes.some((n) => n.id === "agent:unrelated")).toBe(false);
  });

  it("returns an empty graph when the agent isn't present", () => {
    expect(agentSubgraph(G, "ghost").nodes).toHaveLength(0);
  });
});
