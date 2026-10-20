import { describe, expect, it } from "vitest";

import type { PolicyGraph, PolicyGraphEdge } from "./api";
import { COL_W, HEX_W, ROW_H, layeredLayout } from "./graph-layout";

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
    // Each heading sits on its own column's top node.
    expect(columns[0].top).toBe(pos.r.y - ROW_H / 2);
    expect(columns.map((c) => c.title)).toEqual([
      "Caller roles",
      "Agents",
      "Agents · hop 1",
      "Tools",
    ]);
  });

  it("starts a cycle nothing enters at its first agent", () => {
    const g = graph(
      [
        ["a", "agent"],
        ["b", "agent"],
      ],
      [edge("a", "b", "reach"), edge("b", "a", "reach")],
    );
    const { col } = layeredLayout(g);
    expect(col).toEqual({ a: 1, b: 2 });
  });

  it("keeps a chain whose entry calls itself", () => {
    const g = graph(
      [
        ["r", "role"],
        ["a", "agent"],
        ["b", "agent"],
        ["c", "agent"],
      ],
      [
        edge("r", "a", "admission"),
        edge("a", "a", "reach"),
        edge("a", "b", "reach"),
        edge("b", "c", "reach"),
      ],
    );
    expect(layeredLayout(g).col).toMatchObject({ a: 1, b: 2, c: 3 });
  });

  it("doesn't count a denied reach as a hop", () => {
    const denied = { ...edge("a", "b", "reach"), verdict: "deny" as const };
    const g = graph(
      [
        ["a", "agent"],
        ["b", "agent"],
      ],
      [denied],
    );
    expect(layeredLayout(g).col).toEqual({ a: 1, b: 1 });
  });

  it("bends an edge through each column it crosses", () => {
    // a calls t past b's column: the edge gets a lane there, off b's row.
    const g = graph(
      [
        ["a", "agent"],
        ["b", "agent"],
        ["t", "tool"],
      ],
      [edge("a", "b", "reach"), edge("a", "t", "call"), edge("b", "t", "call")],
    );
    const { pos, bends } = layeredLayout(g);
    expect(bends[1]).toHaveLength(1);
    expect(bends[1][0].x).toBe(pos.b.x);
    expect(bends[1][0].y).not.toBe(pos.b.y);
    expect(bends[0]).toEqual([]);
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

  it("loops a same-column edge out to the right", () => {
    // Both agents are admitted, so a's reach to b stays in one column.
    const g = graph(
      [
        ["r", "role"],
        ["a", "agent"],
        ["b", "agent"],
      ],
      [
        edge("r", "a", "admission"),
        edge("r", "b", "admission"),
        edge("a", "b", "reach"),
      ],
    );
    const { col, pos, bends } = layeredLayout(g);
    expect(col.a).toBe(col.b);
    // Down a lane just clear of the hexes' side tips.
    const lane = bends[2][0].x;
    expect(lane).toBeGreaterThan(pos.a.x + HEX_W / 2);
    expect(bends[2]).toEqual([
      { x: lane, y: pos.a.y },
      { x: lane, y: pos.b.y },
    ]);
  });

  it("gives each loop in a column its own lane", () => {
    const g = graph(
      [
        ["r", "role"],
        ["a", "agent"],
        ["b", "agent"],
        ["c", "agent"],
      ],
      [
        edge("r", "a", "admission"),
        edge("r", "b", "admission"),
        edge("r", "c", "admission"),
        edge("a", "c", "reach"),
        edge("b", "c", "reach"),
        edge("a", "a", "reach"),
      ],
    );
    const { pos, bends } = layeredLayout(g);
    const lanes = [3, 4, 5].map((i) => bends[i][0].x);
    expect(new Set(lanes).size).toBe(3);
    // All between the column's hexes and the next column's.
    for (const x of lanes) {
      expect(x).toBeGreaterThan(pos.a.x + HEX_W / 2);
      expect(x).toBeLessThan(pos.a.x + COL_W - HEX_W / 2);
    }
  });

  it("fans apart edges joining the same two nodes", () => {
    // a calls t and reaches it as a tool: two curves, not one on another.
    const g = graph(
      [
        ["r", "role"],
        ["a", "agent"],
        ["b", "agent"],
        ["t", "tool"],
      ],
      [
        edge("r", "a", "admission"),
        edge("a", "t", "call"),
        { ...edge("a", "t", "reach"), via: "tool" },
      ],
    );
    const { bends } = layeredLayout(g);
    expect(bends[1]).toHaveLength(1);
    expect(bends[2]).toHaveLength(1);
    expect(bends[1][0].y).not.toBe(bends[2][0].y);
    // A lone edge between adjacent columns needs no bend.
    expect(bends[0]).toEqual([]);
  });

  it("keeps headings above any lane in their column", () => {
    const g = graph(
      [
        ["a", "agent"],
        ["b", "agent"],
        ["t", "tool"],
      ],
      [edge("a", "b", "reach"), edge("a", "t", "call")],
    );
    const { columns, bends } = layeredLayout(g);
    const hop = columns.find((c) => c.title === "Agents · hop 1")!;
    expect(hop.top).toBeLessThanOrEqual(bends[1][0].y - 16);
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
