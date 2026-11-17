/**
 * Keeps a large policy graph readable: past a tool budget, each agent lists
 * its first few tools and folds the rest into one "+N tools" node, which a
 * click expands. Pure, so it's testable without React Flow.
 */

import type { PolicyGraph, PolicyGraphEdge, PolicyGraphNode } from "./api";

/** Collapse only once the graph shows more tools than this. */
export const TOOL_BUDGET = 12;
/** Tools an agent keeps before the rest fold into its "+N" node. */
export const TOOLS_PER_AGENT = 6;

/** A graph as drawn: the policy graph plus fold nodes and their edges. */
export type ShownNode =
  | PolicyGraphNode
  | { id: string; kind: "more"; label: string; agent: string; count: number };
export type ShownEdge =
  | PolicyGraphEdge
  | (Omit<PolicyGraphEdge, "verdict"> & { verdict: "mixed" });
export type ShownGraph = {
  nodes: ShownNode[];
  edges: ShownEdge[];
  roles: string[];
};

export const MORE_PREFIX = "more:";
const isTool = (kind: string) => kind === "tool" || kind === "mcp";

/** Fold each agent's tools past `perAgent` into a `more:<agent>` node, unless
 * the agent is in `expanded` or the graph has `budget` tools or fewer. A tool
 * stays when any agent still lists it. */
export function collapseTools(
  graph: PolicyGraph,
  expanded: ReadonlySet<string>,
  budget = TOOL_BUDGET,
  perAgent = TOOLS_PER_AGENT,
): ShownGraph {
  const kind = new Map(graph.nodes.map((n) => [n.id, n.kind]));
  const tools = graph.nodes.filter((n) => isTool(n.kind)).length;
  if (tools <= budget) return graph;

  const seen = new Map<string, number>();
  const folded = new Map<string, PolicyGraphEdge[]>();
  const kept: ShownEdge[] = [];
  for (const e of graph.edges) {
    const toTool = e.kind === "call" && isTool(kind.get(e.target) ?? "");
    if (!toTool || expanded.has(e.source)) {
      kept.push(e);
      continue;
    }
    const n = seen.get(e.source) ?? 0;
    seen.set(e.source, n + 1);
    if (n < perAgent) kept.push(e);
    else
      (folded.get(e.source) ?? folded.set(e.source, []).get(e.source)!).push(e);
  }
  const used = new Set(kept.flatMap((e) => [e.source, e.target]));
  const nodes: ShownNode[] = graph.nodes.filter(
    (n) => !isTool(n.kind) || used.has(n.id),
  );
  const edges = [...kept];
  for (const [agent, hidden] of folded) {
    const id = `${MORE_PREFIX}${agent}`;
    nodes.push({
      id,
      kind: "more",
      label: `+${hidden.length} tools`,
      agent,
      count: hidden.length,
    });
    const verdicts = new Set(hidden.map((e) => e.verdict));
    edges.push({
      source: agent,
      target: id,
      kind: "call",
      via: null,
      verdict: verdicts.size === 1 ? hidden[0].verdict : "mixed",
      constraints: [],
      roles: [...new Set(hidden.flatMap((e) => e.roles))],
    });
  }
  return { nodes, edges, roles: graph.roles };
}

/** Every node on a directed path to or from `id`, and the edges on them. */
export function pathsThrough(
  graph: {
    nodes: { id: string }[];
    edges: { source: string; target: string }[];
  },
  id: string,
): { nodes: Set<string>; edges: Set<number> } {
  const out = new Map<string, string[]>();
  const inc = new Map<string, string[]>();
  for (const e of graph.edges) {
    (out.get(e.source) ?? out.set(e.source, []).get(e.source)!).push(e.target);
    (inc.get(e.target) ?? inc.set(e.target, []).get(e.target)!).push(e.source);
  }
  const bfs = (from: Map<string, string[]>) => {
    const seen = new Set([id]);
    const q = [id];
    for (let i = 0; i < q.length; i++)
      for (const v of from.get(q[i]) ?? [])
        if (!seen.has(v)) {
          seen.add(v);
          q.push(v);
        }
    return seen;
  };
  const fwd = bfs(out);
  const bwd = bfs(inc);
  const edges = new Set<number>();
  graph.edges.forEach((e, i) => {
    if (fwd.has(e.source) || bwd.has(e.target)) edges.add(i);
  });
  return { nodes: new Set([...fwd, ...bwd]), edges };
}

/** Only the nodes and edges on a path through `id`. */
export function isolate(graph: ShownGraph, id: string): ShownGraph {
  const { nodes, edges } = pathsThrough(graph, id);
  return {
    nodes: graph.nodes.filter((n) => nodes.has(n.id)),
    edges: graph.edges.filter((_, i) => edges.has(i)),
    roles: graph.roles,
  };
}
