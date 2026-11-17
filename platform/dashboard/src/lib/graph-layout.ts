/**
 * Layered layout for the policy graph: roles, then agents (one column per
 * permitted reach hop, so agent-as-tool chains read left to right), then
 * tools. An edge that spans columns bends through a reserved slot in each
 * column it crosses, and one within a column loops out to its right, so no
 * edge runs under a node it doesn't touch.
 * Pure and deterministic, so it's testable without React Flow.
 */

import type { PolicyGraph } from "./api";

/** A node's box (the graph's hexagon); positions are node centres. */
export const HEX_W = 136;
export const HEX_H = 118;
/** Least column pitch, and row pitch; both leave room for lanes beside the
 * hexes. A column with more loops than fit grows wider. */
export const COL_W = 300;
export const ROW_H = 150;
/** First loop lane right of a column's centre, clear of the hex's side tip. */
const LOOP_X = HEX_W / 2 + 16;
/** Gap between loop lanes: wider than a lane's label, so two never overlap. */
export const LANE_W = 64;
/** Height of a bend slot: a lane an edge passes through. */
const BEND_H = 32;
/** Gap between edges that join the same two nodes. */
const SPREAD = 24;

type Point = { x: number; y: number };

export interface LayeredLayout {
  /** Centre of each node. */
  pos: Record<string, Point>;
  /** Column index per node. */
  col: Record<string, number>;
  /** Heading, x and top of each non-empty column, left to right. */
  columns: { title: string; x: number; top: number }[];
  /** Bend points per edge index (empty for adjacent columns). */
  bends: Record<number, Point[]>;
  /** Vertical offset of an edge's arrival at its target, so edges joining the
   * same two nodes don't share one arrowhead. */
  endDy: Record<number, number>;
}

/** Whether an edge lets its source delegate to its target: a permitted
 * reach. A denied one delegates nothing. */
export const delegates = (e: { kind: string; verdict: string }) =>
  e.kind === "reach" && e.verdict !== "deny";

/** Append `v` to the list at `k`, starting it if need be. */
export const push = <K, V>(m: Map<K, V[]>, k: K, v: V) => {
  const a = m.get(k);
  if (a) a.push(v);
  else m.set(k, [v]);
};

/** Column per node — roles 0, agents 1 + their shortest permitted reach
 * depth, tools and MCP servers last — and the column count. */
function columnsOf(graph: PolicyGraph): {
  col: Record<string, number>;
  nCols: number;
} {
  const agents = graph.nodes.filter((n) => n.kind === "agent").map((n) => n.id);
  // Only a permitted reach delegates; a self-loop adds no hop.
  const out = new Map<string, string[]>();
  const reached = new Set<string>();
  for (const e of graph.edges) {
    if (!delegates(e) || e.source === e.target) continue;
    push(out, e.source, e.target);
    reached.add(e.target);
  }
  const admitted = new Set(
    graph.edges
      .filter((e) => e.kind === "admission" && e.verdict !== "deny")
      .map((e) => e.target),
  );

  // BFS from entry agents (admitted, or reached by nobody); then from any
  // agent still unplaced (a cycle nothing enters), so every agent lands.
  const depth = new Map<string, number>();
  const bfs = (seeds: string[]) => {
    const queue = seeds.filter((id) => !depth.has(id));
    for (const id of queue) depth.set(id, 0);
    for (let i = 0; i < queue.length; i++) {
      for (const v of out.get(queue[i]) ?? []) {
        if (!depth.has(v)) {
          depth.set(v, depth.get(queue[i])! + 1);
          queue.push(v);
        }
      }
    }
  };
  bfs(agents.filter((id) => admitted.has(id) || !reached.has(id)));
  for (const id of agents) if (!depth.has(id)) bfs([id]);

  const maxAgent = Math.max(0, ...agents.map((id) => depth.get(id)!));
  const col: Record<string, number> = {};
  for (const n of graph.nodes) {
    col[n.id] =
      n.kind === "role"
        ? 0
        : n.kind === "agent"
          ? 1 + depth.get(n.id)!
          : 2 + maxAgent;
  }
  return { col, nCols: maxAgent + 3 };
}

export function layeredLayout(graph: PolicyGraph): LayeredLayout {
  const { col, nCols } = columnsOf(graph);

  // Each column holds nodes and bend slots, in the server's (stable) order.
  const layers: string[][] = Array.from({ length: nCols }, () => []);
  for (const n of graph.nodes) layers[col[n.id]].push(n.id);

  const prev = new Map<string, string[]>();
  const next = new Map<string, string[]>();
  const bendIds: Record<number, { id: string; c: number }[]> = {};
  graph.edges.forEach((e, i) => {
    const a = col[e.source];
    const b = col[e.target];
    // Same-column edges loop beside the column; they don't order it.
    if (a == null || b == null || a === b) return;
    // A chain source → slot → … → target, one slot per crossed column.
    const step = b > a ? 1 : -1;
    const chain = [e.source];
    const slots: { id: string; c: number }[] = [];
    for (let c = a + step; c !== b; c += step) {
      const id = `bend:${i}:${c}`;
      layers[c].push(id);
      slots.push({ id, c });
      chain.push(id);
    }
    chain.push(e.target);
    bendIds[i] = slots;
    for (let k = 1; k < chain.length; k++) {
      // Left-to-right neighbours, whichever way the edge points.
      const [l, r] =
        step > 0 ? [chain[k - 1], chain[k]] : [chain[k], chain[k - 1]];
      push(next, l, r);
      push(prev, r, l);
    }
  });

  const height = (id: string) => (id.startsWith("bend:") ? BEND_H : ROW_H);
  const colHeight = (layer: string[]) =>
    layer.reduce((h, id) => h + height(id), 0);
  // Centre y per node, each column centred on the tallest.
  const tallest = Math.max(ROW_H, ...layers.map(colHeight));
  const y = new Map<string, number>();
  const place = (c: number) => {
    let at = (tallest - colHeight(layers[c])) / 2;
    for (const id of layers[c]) {
      y.set(id, at + height(id) / 2);
      at += height(id);
    }
  };
  layers.forEach((_, c) => place(c));

  // Barycenter sweeps (down then up) on on-screen y, to cut crossings.
  for (let sweep = 0; sweep < 4; sweep++) {
    const down = sweep % 2 === 0;
    const order = layers.map((_, c) => c);
    const cols = down ? order.slice(1) : order.slice(0, -1).reverse();
    for (const c of cols) {
      const keyed = layers[c].map((id, i) => {
        const ys = ((down ? prev : next).get(id) ?? []).map((n) => y.get(n)!);
        const k = ys.length
          ? ys.reduce((s, v) => s + v, 0) / ys.length
          : y.get(id)!;
        return { id, k, i };
      });
      keyed.sort((a, b) => a.k - b.k || a.i - b.i);
      layers[c] = keyed.map((x) => x.id);
      place(c);
    }
  }

  // Each loop (same column, or self) gets its own lane beside its column,
  // so unrelated loops never share a vertical run; a column with more loops
  // than fit in COL_W pushes the next columns right.
  const isLoop = (e: { source: string; target: string }) =>
    col[e.source] != null && col[e.source] === col[e.target];
  const loops = Array.from({ length: nCols }, () => 0);
  for (const e of graph.edges) if (isLoop(e)) loops[col[e.source]]++;
  // A column is as wide as its lanes plus the next column's half hex.
  const width = (c: number) =>
    Math.max(COL_W, LOOP_X + loops[c] * LANE_W + HEX_W / 2);
  const colX = [0];
  for (let c = 1; c < nCols; c++) colX[c] = colX[c - 1] + width(c - 1);

  const pos: LayeredLayout["pos"] = {};
  for (const n of graph.nodes) {
    pos[n.id] = { x: colX[col[n.id]], y: y.get(n.id)! };
  }
  // Edges joining the same two nodes (either way) fan apart by SPREAD.
  const keys = graph.edges.map((e) =>
    [e.source, e.target].sort().join("\u0000"),
  );
  const pairSize = new Map<string, number>();
  for (const key of keys) pairSize.set(key, (pairSize.get(key) ?? 0) + 1);
  const pairSeen = new Map<string, number>();
  const loopLanes = new Map<number, number>();
  const bends: LayeredLayout["bends"] = {};
  const endDy: LayeredLayout["endDy"] = {};
  graph.edges.forEach((e, i) => {
    const s = pos[e.source];
    const t = pos[e.target];
    if (!s || !t) return;
    const j = pairSeen.get(keys[i]) ?? 0;
    pairSeen.set(keys[i], j + 1);
    const n = pairSize.get(keys[i])!;
    const spread = (j - (n - 1) / 2) * SPREAD;
    if (n > 1) endDy[i] = spread / 2;
    if (isLoop(e)) {
      const c = col[e.source];
      const k = loopLanes.get(c) ?? 0;
      loopLanes.set(c, k + 1);
      const out = s.x + LOOP_X + k * LANE_W;
      // A self-loop runs out and back beside its own node.
      const [y1, y2] =
        e.source === e.target ? [s.y - ROW_H / 4, s.y + ROW_H / 4] : [s.y, t.y];
      bends[i] = [
        { x: out, y: y1 },
        { x: out, y: y2 },
      ];
    } else if (bendIds[i]?.length) {
      bends[i] = bendIds[i].map(({ id, c }) => ({
        x: colX[c],
        y: y.get(id)!,
      }));
    } else {
      bends[i] =
        n > 1 ? [{ x: (s.x + t.x) / 2, y: (s.y + t.y) / 2 + spread }] : [];
    }
  });

  // Titles by position (tools and MCP servers are always last); bend-only
  // columns get none.
  const kind = new Map(graph.nodes.map((n) => [n.id, n.kind]));
  const columns = layers.flatMap((layer, c) => {
    if (!layer.some((id) => !id.startsWith("bend:"))) return [];
    const onlyMcp = layer.every(
      (id) => id.startsWith("bend:") || kind.get(id) === "mcp",
    );
    const title =
      c === 0
        ? "Caller roles"
        : c === nCols - 1
          ? onlyMcp
            ? "MCP servers"
            : "Tools"
          : c === 1
            ? "Agents"
            : `Agents · hop ${c - 1}`;
    // Lanes count, so a heading never sits on an edge.
    const top = Math.min(...layer.map((id) => y.get(id)! - height(id) / 2));
    return [{ title, x: colX[c], top }];
  });
  return { pos, col, columns, bends, endDy };
}
