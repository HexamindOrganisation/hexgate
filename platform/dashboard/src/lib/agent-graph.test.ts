import { describe, expect, it } from "vitest";

import type { PolicyGraph } from "@/lib/api";
import { agentSubgraph } from "./agent-graph";

const G: PolicyGraph = {
  roles: ["support"],
  nodes: [
    { id: "agent:a", kind: "agent", label: "a" },
    { id: "tool:t", kind: "tool", label: "t" },
    { id: "agent:b", kind: "agent", label: "b" },
    { id: "role:support", kind: "role", label: "support" },
  ],
  edges: [
    {
      source: "role:support",
      target: "agent:a",
      kind: "admission",
      via: null,
      verdict: "allow",
      constraints: [],
      roles: ["support"],
    },
    {
      source: "agent:a",
      target: "tool:t",
      kind: "call",
      via: null,
      verdict: "allow",
      constraints: [],
      roles: [],
    },
    {
      source: "agent:b",
      target: "tool:t",
      kind: "call",
      via: null,
      verdict: "allow",
      constraints: [],
      roles: [],
    },
  ],
};

describe("agentSubgraph", () => {
  it("keeps the agent and its one-hop neighbours only", () => {
    const sub = agentSubgraph(G, "a");
    expect(sub.nodes.map((n) => n.id).sort()).toEqual([
      "agent:a",
      "role:support",
      "tool:t",
    ]);
    expect(sub.edges).toHaveLength(2);
    // The unrelated agent b (and its edge) is excluded.
    expect(sub.nodes.some((n) => n.id === "agent:b")).toBe(false);
  });

  it("returns an empty graph when the agent isn't present", () => {
    expect(agentSubgraph(G, "ghost").nodes).toHaveLength(0);
  });
});
