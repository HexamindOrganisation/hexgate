import type { PolicyGraph } from "@/lib/api";

/** The agent's neighbourhood in the project graph: the agent node plus every
 * node one edge away (its roles, the tools it calls and the agents it reaches
 * or is reached by). Pure, so the hub can scope the graph with no backend
 * change. Returns an empty graph when the agent isn't in it. */
export function agentSubgraph(
  graph: PolicyGraph,
  agentName: string,
): PolicyGraph {
  const node = graph.nodes.find(
    (n) => n.kind === "agent" && n.label === agentName,
  );
  if (!node) return { nodes: [], edges: [], roles: graph.roles };
  const edges = graph.edges.filter(
    (e) => e.source === node.id || e.target === node.id,
  );
  const keep = new Set<string>([node.id]);
  for (const e of edges) {
    keep.add(e.source);
    keep.add(e.target);
  }
  return {
    nodes: graph.nodes.filter((n) => keep.has(n.id)),
    edges,
    roles: graph.roles,
  };
}
