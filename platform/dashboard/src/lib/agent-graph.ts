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

export type AgentReach = { reaches: string[]; calledBy: string[] };

/** The delegation neighbours of every agent the graph names, plus `names`
 * (e.g. registered agents it doesn't), resolved as the hub does: an agent's
 * own edges when the policy declares it, else the generic `*` agent's. Only
 * a permitted reach delegates (a call edge never targets an agent), and a
 * reach to itself is none. A reach to `*` reads "any agent" and makes it a
 * caller of every other agent. Labels are deduped, in graph order. */
export function agentReachMap(
  graph: PolicyGraph,
  names: string[] = [],
): Map<string, AgentReach> {
  const STAR = "agent:*";
  const label = new Map(
    graph.nodes.filter((n) => n.kind === "agent").map((n) => [n.id, n.label]),
  );
  const all = new Set([
    ...[...label].filter(([id]) => id !== STAR).map(([, l]) => l),
    ...names,
  ]);
  // Declared, by resolveAgent's rule: edges out, or roles that admit it.
  const declared = new Set<string>();
  const out = new Map<string, PolicyGraph["edges"]>();
  for (const e of graph.edges) {
    declared.add(e.source);
    if (e.kind === "admission") declared.add(e.target);
    if (delegates(e)) push(out, e.source, e);
  }
  const hasStar = label.has(STAR);
  const reaches = new Map<string, Set<string>>();
  const calledBy = new Map<string, Set<string>>();
  const add = (m: Map<string, Set<string>>, k: string, v: string) =>
    (m.get(k) ?? m.set(k, new Set()).get(k)!).add(v);
  for (const name of all) {
    const own = `agent:${name}`;
    const source =
      declared.has(own) || !hasStar ? (label.has(own) ? own : null) : STAR;
    for (const e of (source && out.get(source)) || []) {
      const to = label.get(e.target);
      if (to === undefined || to === name) continue;
      if (e.target === STAR) {
        add(reaches, name, "any agent");
        for (const other of all) if (other !== name) add(calledBy, other, name);
        continue;
      }
      add(reaches, name, to);
      add(calledBy, to, name);
    }
  }
  const m = new Map<string, AgentReach>();
  for (const name of new Set([...reaches.keys(), ...calledBy.keys()])) {
    m.set(name, {
      reaches: [...(reaches.get(name) ?? [])],
      calledBy: [...(calledBy.get(name) ?? [])],
    });
  }
  return m;
}
