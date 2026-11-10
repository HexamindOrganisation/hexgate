import type { PolicyGraph } from "@/lib/api";
import { delegates, push } from "@/lib/graph-layout";

/** The agent's node, and whose outbound edges stand for it: its own
 * `agent:<name>` when the policy declares it (it has edges out, or roles that
 * admit it), else the generic `agent:*` it resolves like, relabelled to the
 * name. An agent that only appears as another's reach target isn't declared. */
function resolveAgent(graph: PolicyGraph, agentName: string) {
  const own = graph.nodes.find((n) => n.id === `agent:${agentName}`);
  const star = graph.nodes.find((n) => n.id === "agent:*");
  const declared =
    own &&
    graph.edges.some(
      (e) =>
        e.source === own.id || (e.target === own.id && e.kind === "admission"),
    );
  const source = declared || !star ? own : star;
  if (!source) return null;
  return { node: { ...(own ?? star!), label: agentName }, sourceId: source.id };
}

/** The agent's neighbourhood in the project graph: the agent, every node one
 * edge away (its roles, the tools it calls, the agents it reaches or that
 * reach it), plus what it can delegate to: each agent its permitted reach
 * leads to, hop by hop, with that agent's own edges. A denied reach delegates
 * nothing, so it's shown but not followed. Pure, so the hub can scope the
 * graph with no backend change. Empty when the agent isn't in it. */
export function agentSubgraph(
  graph: PolicyGraph,
  agentName: string,
): PolicyGraph {
  const found = resolveAgent(graph, agentName);
  if (!found) return { nodes: [], edges: [], roles: graph.roles };
  const { node, sourceId } = found;
  const out = new Map<string, PolicyGraph["edges"]>();
  for (const e of graph.edges) push(out, e.source, e);
  // The agent's own edges: what it may use (its stand-in's, re-pointed at it,
  // when that's `*`), and who reaches or admits it.
  const own = (out.get(sourceId) ?? []).map((e) =>
    sourceId === node.id ? e : { ...e, source: node.id },
  );
  const edges = new Set([
    ...own,
    ...graph.edges
      .filter(
        (e) =>
          e.target === node.id ||
          (e.target === sourceId && e.kind === "admission"),
      )
      .map((e) => (e.target === node.id ? e : { ...e, target: node.id })),
  ]);
  const keep = new Set<string>([node.id]);
  for (const e of edges) {
    keep.add(e.source);
    keep.add(e.target);
  }
  // Follow permitted reach out of the agent, so a handoff's tools show; a
  // caller that reaches INTO the agent doesn't drag its own tools in.
  const seen = new Set([node.id, sourceId]);
  const queue: string[] = [];
  for (const e of own) {
    if (delegates(e) && !seen.has(e.target)) {
      seen.add(e.target);
      queue.push(e.target);
    }
  }
  for (let i = 0; i < queue.length; i++) {
    for (const e of out.get(queue[i]) ?? []) {
      edges.add(e);
      keep.add(e.target);
      if (delegates(e) && !seen.has(e.target)) {
        seen.add(e.target);
        queue.push(e.target);
      }
    }
  }
  const nodes = graph.nodes.filter((n) => keep.has(n.id) && n.id !== node.id);
  return { nodes: [node, ...nodes], edges: [...edges], roles: graph.roles };
}

/** What the agent itself may use, from its own outbound edges in `sub`: tools,
 * MCP tools and agents, each counted once and never through a denied edge. */
export function agentReachCounts(sub: PolicyGraph) {
  // agentSubgraph puts the agent first.
  const self = sub.nodes[0];
  const kind = new Map(sub.nodes.map((n) => [n.id, n.kind]));
  // Never through a denied edge, and a reach to itself is no reach.
  const open = sub.edges.filter(
    (e) =>
      e.source === self?.id && e.verdict !== "deny" && e.target !== self.id,
  );
  const count = (k: string) =>
    new Set(open.filter((e) => kind.get(e.target) === k).map((e) => e.target))
      .size;
  return {
    tools: count("tool"),
    mcpTools: count("mcp"),
    agents: count("agent"),
  };
}
