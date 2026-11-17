import type { PolicyGraph, PolicyGraphEdge } from "@/lib/api";
import { delegates, push } from "@/lib/graph-layout";

/** The agent's node, and whose outbound edges stand for it: its own
 * `agent:<name>` when the policy declares it, else the generic `agent:*` it
 * resolves like, relabelled to the name. Only an agent that appears solely as
 * another's reach target is undeclared; one with no edges at all is locked
 * down to nothing (a named cell replaces `*`). */
function resolveAgent(graph: PolicyGraph, agentName: string) {
  const own = graph.nodes.find((n) => n.id === `agent:${agentName}`);
  const star = graph.nodes.find((n) => n.id === "agent:*");
  const touching = own
    ? graph.edges.filter((e) => e.source === own.id || e.target === own.id)
    : [];
  const declared =
    own &&
    (touching.length === 0 ||
      touching.some((e) => !(e.target === own.id && e.kind === "reach")));
  const source = declared || !star ? own : star;
  if (!source) return null;
  return { node: { ...(own ?? star!), label: agentName }, sourceId: source.id };
}

/** One agent's graph: the roles that run it, the agent, and its own
 * outbound edges (the tools it calls, the sub-agents it reaches). One hop
 * only: a sub-agent is a node, not its tools, and agents that call this one
 * aren't drawn; each has a graph of its own. Pure, so the hub can scope the
 * project graph with no backend change. Empty when the agent isn't in it. */
export function agentSubgraph(
  graph: PolicyGraph,
  agentName: string,
): PolicyGraph {
  const found = resolveAgent(graph, agentName);
  if (!found) return { nodes: [], edges: [], roles: graph.roles };
  const { node, sourceId } = found;
  // Its stand-in's edges (`*`'s) are re-pointed at it, and a reach to itself
  // is no sub-agent.
  const repoint = (e: PolicyGraphEdge) => ({
    ...e,
    source: e.source === sourceId ? node.id : e.source,
    target: e.target === sourceId ? node.id : e.target,
  });
  const edges = graph.edges
    .filter(
      (e) =>
        (e.source === sourceId && e.target !== sourceId) ||
        ((e.target === node.id || e.target === sourceId) &&
          e.kind === "admission"),
    )
    .map(repoint);
  const keep = new Set(edges.flatMap((e) => [e.source, e.target]));
  const nodes = graph.nodes.filter((n) => keep.has(n.id) && n.id !== node.id);
  return { nodes: [node, ...nodes], edges, roles: graph.roles };
}

/** How many tools each agent may call (never through a denied edge), by
 * label, resolved as the hub does; for a sub-agent's "N tools" caption. */
export function toolCountOf(graph: PolicyGraph, agentName: string): number {
  const found = resolveAgent(graph, agentName);
  if (!found) return 0;
  const kind = new Map(graph.nodes.map((n) => [n.id, n.kind]));
  return new Set(
    graph.edges
      .filter(
        (e) =>
          e.source === found.sourceId &&
          e.kind === "call" &&
          e.verdict !== "deny" &&
          (kind.get(e.target) === "tool" || kind.get(e.target) === "mcp"),
      )
      .map((e) => e.target),
  ).size;
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
 * reach to itself is none. A reach to `*` is none either: the engine has no
 * wildcard reach, so it denies every real target. Labels are deduped, in graph order. */
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
  // Declared, by resolveAgent's rule: any edge but an inbound reach, or none
  // at all (locked down to nothing).
  const touched = new Set<string>();
  const declared = new Set<string>();
  const out = new Map<string, PolicyGraph["edges"]>();
  for (const e of graph.edges) {
    touched.add(e.source).add(e.target);
    declared.add(e.source);
    if (e.kind !== "reach") declared.add(e.target);
    if (delegates(e)) push(out, e.source, e);
  }
  const hasStar = label.has(STAR);
  const reaches = new Map<string, Set<string>>();
  const calledBy = new Map<string, Set<string>>();
  const add = (m: Map<string, Set<string>>, k: string, v: string) =>
    (m.get(k) ?? m.set(k, new Set()).get(k)!).add(v);
  for (const name of all) {
    const own = `agent:${name}`;
    const isDeclared =
      label.has(own) && (declared.has(own) || !touched.has(own));
    const source =
      isDeclared || !hasStar ? (label.has(own) ? own : null) : STAR;
    for (const e of (source && out.get(source)) || []) {
      const to = label.get(e.target);
      if (to === undefined || to === name || e.target === STAR) continue;
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

/** The agent's outbound reach edges, resolved as the hub does (its own, or
 * the generic `*` agent's when the policy doesn't declare it), keyed
 * `<target>:<via>` (the graph merges one edge per pair across roles). Edges
 * with no via can't be matched, so are skipped. */
export function agentReachEdges(
  graph: PolicyGraph,
  agentName: string,
): Map<string, PolicyGraphEdge> {
  const out = new Map<string, PolicyGraphEdge>();
  const found = resolveAgent(graph, agentName);
  if (!found) return out;
  const label = new Map(
    graph.nodes.filter((n) => n.kind === "agent").map((n) => [n.id, n.label]),
  );
  for (const e of graph.edges) {
    const to = label.get(e.target);
    if (e.source === found.sourceId && to !== undefined && e.via) {
      out.set(`${to}:${e.via}`, e);
    }
  }
  return out;
}
