/**
 * Layered layout for the policy graph: roles, then agents (one column per
 * reach hop, so agent-as-tool chains read left to right), then tools.
 * Pure and deterministic, so it's testable without React Flow.
 */

import type { PolicyGraph } from "./api";

export const COL_W = 300;
export const ROW_H = 150;

export interface LayeredLayout {
  pos: Record<string, { x: number; y: number }>;
  /** Column index per node. */
  col: Record<string, number>;
  /** Heading and x per non-empty column, left to right. */
  columns: { title: string; x: number }[];
}

/** Column per node: roles 0, agents 1 + their shortest reach depth, tools
 * and MCP servers after the deepest agent. */
function columnsOf(graph: PolicyGraph): Record<string, number> {
  const agents = graph.nodes.filter((n) => n.kind === "agent");
  const reach = graph.edges.filter((e) => e.kind === "reach");
  const out = new Map<string, string[]>();
  for (const e of reach)
    out.set(e.source, [...(out.get(e.source) ?? []), e.target]);
  const reached = new Set(reach.map((e) => e.target));
  // Entry agents: nobody reaches them. A pure cycle has none, so all start.
  let roots = agents.filter((a) => !reached.has(a.id)).map((a) => a.id);
  if (roots.length === 0) roots = agents.map((a) => a.id);

  // Shortest hop count (BFS), so a cycle can't push a column out forever.
  const depth = new Map<string, number>(roots.map((id) => [id, 0]));
  const queue = [...roots];
  for (let i = 0; i < queue.length; i++) {
    const u = queue[i];
    for (const v of out.get(u) ?? []) {
      if (!depth.has(v)) {
        depth.set(v, depth.get(u)! + 1);
        queue.push(v);
      }
    }
  }

  const maxAgent = Math.max(0, ...agents.map((a) => depth.get(a.id) ?? 0));
  const col: Record<string, number> = {};
  for (const n of graph.nodes) {
    col[n.id] =
      n.kind === "role"
        ? 0
        : n.kind === "agent"
          ? 1 + (depth.get(n.id) ?? 0)
          : 2 + maxAgent;
  }
  return col;
}

/** Mean row of a node's neighbours in `rows`, or its own row if it has none. */
function barycenter(
  id: string,
  neighbours: Map<string, string[]>,
  rows: Map<string, number>,
  fallback: number,
): number {
  const ys = (neighbours.get(id) ?? [])
    .map((n) => rows.get(n))
    .filter((y): y is number => y != null);
  return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : fallback;
}

export function layeredLayout(graph: PolicyGraph): LayeredLayout {
  const col = columnsOf(graph);
  // Fixed shape: roles, agent hops, tools — even when a column is empty.
  const nCols =
    Math.max(
      1,
      ...graph.nodes.filter((n) => n.kind === "agent").map((n) => col[n.id]),
    ) + 2;

  // Initial order: as the server listed them, which is stable.
  const layers: string[][] = Array.from({ length: nCols }, () => []);
  for (const n of graph.nodes) layers[col[n.id]].push(n.id);

  const prev = new Map<string, string[]>();
  const next = new Map<string, string[]>();
  for (const e of graph.edges) {
    next.set(e.source, [...(next.get(e.source) ?? []), e.target]);
    prev.set(e.target, [...(prev.get(e.target) ?? []), e.source]);
  }
  const rowsOf = () => {
    const rows = new Map<string, number>();
    for (const layer of layers) layer.forEach((id, i) => rows.set(id, i));
    return rows;
  };
  // Barycenter sweeps (down then up) to cut edge crossings.
  for (let sweep = 0; sweep < 4; sweep++) {
    const down = sweep % 2 === 0;
    const order = down
      ? layers.map((_, i) => i).slice(1)
      : layers
          .map((_, i) => i)
          .slice(0, -1)
          .reverse();
    for (const c of order) {
      const rows = rowsOf();
      const keyed = layers[c].map((id, i) => ({
        id,
        k: barycenter(id, down ? prev : next, rows, i),
        i,
      }));
      keyed.sort((a, b) => a.k - b.k || a.i - b.i);
      layers[c] = keyed.map((x) => x.id);
    }
  }

  // Centre each column on the tallest one.
  const tallest = Math.max(1, ...layers.map((l) => l.length));
  const pos: LayeredLayout["pos"] = {};
  layers.forEach((layer, c) => {
    const top = ((tallest - layer.length) * ROW_H) / 2;
    layer.forEach((id, i) => (pos[id] = { x: c * COL_W, y: top + i * ROW_H }));
  });

  // Titles by position (tools are always last); empty columns get none.
  const columns = layers.flatMap((layer, c) =>
    layer.length === 0
      ? []
      : [
          {
            title:
              c === 0
                ? "Caller roles"
                : c === nCols - 1
                  ? "Tools"
                  : c === 1
                    ? "Agents"
                    : `Agents · hop ${c - 1}`,
            x: c * COL_W,
          },
        ],
  );
  return { pos, col, columns };
}
