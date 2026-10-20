import { describe, expect, it } from "vitest";

import type { PolicyGraph, PolicyGraphEdge } from "./api";
import { COL_W, layeredLayout } from "./graph-layout";

const edge = (
  source: string,
  target: string,
  kind: PolicyGraphEdge["kind"],
): PolicyGraphEdge => ({
  source,
  target,
  kind,
  via: kind === "reach" ? "tool" : null,
  verdict: "allow",
  constraints: [],
  roles: [],
});

function graph(
  nodes: [string, PolicyGraph["nodes"][number]["kind"]][],
  edges: PolicyGraphEdge[],
): PolicyGraph {
  return {
    nodes: nodes.map(([id, kind]) => ({ id, kind, label: id })),
    edges,
    roles: [],
  };
}

describe("layeredLayout", () => {
  it("puts roles, agents and tools in columns, chains one hop further", () => {
    const g = graph(
      [
        ["r", "role"],
        ["a", "agent"],
        ["b", "agent"],
        ["t", "tool"],
        ["m", "mcp"],
      ],
      [
        edge("r", "a", "admission"),
        edge("a", "b", "reach"),
        edge("a", "t", "call"),
        edge("b", "m", "call"),
      ],
    );
    const { col, pos, columns } = layeredLayout(g);
    expect(col).toEqual({ r: 0, a: 1, b: 2, t: 3, m: 3 });
    expect(pos.b.x).toBe(2 * COL_W);
    expect(columns.map((c) => c.title)).toEqual([
      "Caller roles",
      "Agents",
      "Agents · hop 1",
      "Tools",
    ]);
  });

  it("survives a reach cycle", () => {
    const g = graph(
      [
        ["a", "agent"],
        ["b", "agent"],
      ],
      [edge("a", "b", "reach"), edge("b", "a", "reach")],
    );
    const { col } = layeredLayout(g);
    expect(col).toEqual({ a: 1, b: 1 });
  });

  it("names the last column Tools only when it holds tools", () => {
    const g = graph(
      [
        ["r", "role"],
        ["a", "agent"],
      ],
      [edge("r", "a", "admission")],
    );
    expect(layeredLayout(g).columns.map((c) => c.title)).toEqual([
      "Caller roles",
      "Agents",
    ]);
  });

  it("orders a column by its neighbours to avoid crossings", () => {
    // r1 admits a2, r2 admits a1: the agents swap to sit beside their role.
    const g = graph(
      [
        ["r1", "role"],
        ["r2", "role"],
        ["a1", "agent"],
        ["a2", "agent"],
      ],
      [edge("r1", "a2", "admission"), edge("r2", "a1", "admission")],
    );
    const { pos } = layeredLayout(g);
    expect(pos.a2.y).toBeLessThan(pos.a1.y);
  });
});
