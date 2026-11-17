import { describe, expect, it } from "vitest";

import type { PolicyGraph, PolicyGraphEdge } from "./api";
import {
  compactAgentGraph,
  mergeRoleGraphs,
  roleOptionsFor,
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

describe("compactAgentGraph", () => {
  it("draws a few tools as they are", () => {
    const g = agentWith(8);
    expect(compactAgentGraph(g).graph.nodes).toHaveLength(9);
  });

  it("groups tools by verdict past eight", () => {
    const { graph, groups } = compactAgentGraph(agentWith(10, 3));
    expect(graph.nodes.map((n) => n.id).sort()).toEqual([
      "agent:a",
      "group:allow",
      "group:deny",
    ]);
    expect(graph.nodes.find((n) => n.id === "group:allow")?.label).toBe(
      "7 allowed",
    );
    expect(groups.get("deny")).toHaveLength(3);
    // One edge per group, in that group's verdict.
    expect(graph.edges.find((e) => e.target === "group:deny")?.verdict).toBe(
      "deny",
    );
  });

  it("folds roles past five, cutting denied ones first", () => {
    const roles = Array.from({ length: 7 }, (_, i) => `role:r${i}`);
    const g: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:a", kind: "agent", label: "a" },
        ...roles.map((id) => ({ id, kind: "role" as const, label: id })),
      ],
      edges: roles.map((r, i) =>
        edge(r, "agent:a", "admission", {
          verdict: i < 2 ? "deny" : "allow",
        }),
      ),
    };
    const { graph } = compactAgentGraph(g);
    expect(graph.nodes.find((n) => n.id === "more:roles")?.label).toBe(
      "+2 more roles",
    );
    // The two denied roles are the ones cut.
    expect(graph.nodes.some((n) => n.id === "role:r0")).toBe(false);
    expect(graph.nodes.some((n) => n.id === "role:r6")).toBe(true);
  });

  it("folds sub-agents past five into +N, captioned with their tools", () => {
    const subs = Array.from({ length: 6 }, (_, i) => `agent:s${i}`);
    const g: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:a", kind: "agent", label: "a" },
        ...subs.map((id) => ({
          id,
          kind: "agent" as const,
          label: id.slice(6),
        })),
      ],
      edges: subs.map((s) => edge("agent:a", s, "reach")),
    };
    const { graph } = compactAgentGraph(g, () => 4);
    expect(graph.nodes.find((n) => n.id === "more:agents")?.label).toBe(
      "+1 more",
    );
    const s0 = graph.nodes.find((n) => n.id === "agent:s0");
    expect(s0 && "sub" in s0 && s0.sub).toBe("4 tools");
  });
});

describe("mergeRoleGraphs", () => {
  const one = (verdict: PolicyGraphEdge["verdict"], constraints: string[]) => ({
    roles: ["x", "y"],
    nodes: [
      { id: "agent:a", kind: "agent" as const, label: "a" },
      { id: "tool:t", kind: "tool" as const, label: "t" },
    ],
    edges: [edge("agent:a", "tool:t", "call", { verdict, constraints })],
  });

  it("takes the most permissive verdict, as the engine does", () => {
    const m = mergeRoleGraphs([one("deny", []), one("approval_required", [])]);
    expect(m.edges[0].verdict).toBe("approval_required");
  });

  it("keeps either role's condition when both allow", () => {
    const m = mergeRoleGraphs([one("allow", ["a < 1"]), one("allow", ["b"])]);
    expect(m.edges[0].constraints).toEqual(["a < 1 ∨ b"]);
    // An unconditional grant wins outright.
    expect(
      mergeRoleGraphs([one("allow", ["a < 1"]), one("allow", [])]).edges[0]
        .constraints,
    ).toEqual([]);
  });
});

describe("roleOptionsFor", () => {
  const sub: PolicyGraph = {
    roles: ["ops", "guest"],
    nodes: [
      { id: "agent:a", kind: "agent", label: "a" },
      { id: "tool:t", kind: "tool", label: "t" },
      { id: "tool:u", kind: "tool", label: "u" },
    ],
    edges: [
      edge("role:ops", "agent:a", "admission"),
      edge("role:guest", "agent:a", "admission", { verdict: "deny" }),
      edge("agent:a", "tool:t", "call", { roles: ["ops"] }),
      edge("agent:a", "tool:u", "call"),
    ],
  };

  it("says which roles run the agent and how many tools each gets", () => {
    expect(roleOptionsFor(["ops", "guest"], sub)).toEqual([
      { name: "ops", runs: true, tools: 2 },
      { name: "guest", runs: false, tools: 1 },
    ]);
  });

  it("leaves 'runs' unknown when no role gates running it", () => {
    const open = {
      ...sub,
      edges: sub.edges.filter((e) => e.kind !== "admission"),
    };
    expect(roleOptionsFor(["ops"], open)[0].runs).toBeNull();
  });

  describe("round-2 regressions", () => {
    it("says what applies outside a conditional allow", () => {
      const g = (
        verdict: PolicyGraphEdge["verdict"],
        constraints: string[],
      ) => ({
        roles: [],
        nodes: [{ id: "agent:a", kind: "agent" as const, label: "a" }],
        edges: [edge("agent:a", "tool:t", "call", { verdict, constraints })],
      });
      const m = mergeRoleGraphs([
        g("allow", ["x < 5"]),
        g("approval_required", []),
      ]);
      expect(m.edges[0].verdict).toBe("allow");
      expect(m.edges[0].constraints).toEqual([
        "x < 5",
        "otherwise needs approval",
      ]);
    });

    it("counts a sub-agent reached two ways as one slot", () => {
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
      const { graph } = compactAgentGraph(g);
      expect(graph.nodes.some((n) => n.id === "more:agents")).toBe(false);
    });

    it("joins +N more roles to the agent", () => {
      const roles = Array.from({ length: 6 }, (_, i) => `role:r${i}`);
      const g: PolicyGraph = {
        roles: [],
        nodes: [
          { id: "agent:a", kind: "agent", label: "a" },
          ...roles.map((id) => ({ id, kind: "role" as const, label: id })),
        ],
        edges: roles.map((r) => edge(r, "agent:a", "admission")),
      };
      const { graph } = compactAgentGraph(g);
      expect(
        graph.edges.some(
          (e) => e.source === "more:roles" && e.target === "agent:a",
        ),
      ).toBe(true);
    });
  });
});
