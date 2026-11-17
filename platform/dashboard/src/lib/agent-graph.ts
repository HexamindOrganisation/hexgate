import type { PolicyGraph, PolicyGraphEdge } from "@/lib/api";
import { push } from "@/lib/graph-layout";

/** Whether an edge lets its source delegate to its target: a permitted
 * reach. A denied one delegates nothing. */
export const delegates = (e: { kind: string; verdict: string }) =>
  e.kind === "reach" && e.verdict !== "deny";

const STAR = "agent:*";

/** Resolves agents by name against one graph, indexing it once: each one's
 * node, and whose outbound edges stand for it. That's its own
 * `agent:<name>` when the policy declares it, else the generic `agent:*` it
 * resolves like, relabelled to the name. Only an agent that appears solely as
 * another's reach target is undeclared; one with no edges at all is locked
 * down to nothing (a named cell replaces `*`). The one place this rule lives:
 * the hub, /agents and its filters all resolve through it. */
export function agentResolver(graph: PolicyGraph) {
  const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
  // The server says which agents the policy declares; only without that
  // list is it read off the edges.
  const listed = graph.declared_agents && new Set(graph.declared_agents);
  const touched = new Set<string>();
  // Touched by anything but an inbound reach: the policy declares it.
  const declared = new Set<string>();
  for (const e of graph.edges) {
    touched.add(e.source).add(e.target);
    declared.add(e.source);
    if (e.kind !== "reach") declared.add(e.target);
  }
  const star = nodes.get(STAR);
  return (agentName: string) => {
    const own = nodes.get(`agent:${agentName}`);
    const isOwn =
      !!own &&
      (listed
        ? listed.has(agentName)
        : declared.has(own.id) || !touched.has(own.id));
    const source = isOwn || !star ? own : star;
    if (!source) return null;
    return {
      node: { ...(own ?? star!), label: agentName },
      sourceId: source.id,
    };
  };
}

function resolveAgent(graph: PolicyGraph, agentName: string) {
  return agentResolver(graph)(agentName);
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
        // A reach to `*` reaches no one: the engine denies every target.
        (e.source === sourceId && !(e.kind === "reach" && e.target === STAR)) ||
        ((e.target === node.id || e.target === sourceId) &&
          e.kind === "admission"),
    )
    .map(repoint)
    // A reach to itself (through `*` too) is no sub-agent.
    .filter((e) => e.source !== e.target);
  const keep = new Set(edges.flatMap((e) => [e.source, e.target]));
  const nodes = graph.nodes.filter((n) => keep.has(n.id) && n.id !== node.id);
  return { nodes: [node, ...nodes], edges, roles: graph.roles };
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
  const label = new Map(
    graph.nodes.filter((n) => n.kind === "agent").map((n) => [n.id, n.label]),
  );
  const all = new Set([
    ...[...label].filter(([id]) => id !== STAR).map(([, l]) => l),
    ...names,
  ]);
  const out = new Map<string, PolicyGraph["edges"]>();
  for (const e of graph.edges) if (delegates(e)) push(out, e.source, e);
  const resolve = agentResolver(graph);
  const reaches = new Map<string, Set<string>>();
  const calledBy = new Map<string, Set<string>>();
  const add = (m: Map<string, Set<string>>, k: string, v: string) =>
    (m.get(k) ?? m.set(k, new Set()).get(k)!).add(v);
  for (const name of all) {
    const source = resolve(name)?.sourceId;
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

/** How many tools each agent may call (never through a denied edge), by
 * name: one index over `graph`, then a lookup per agent. */
export function toolCounter(graph: PolicyGraph): (agent: string) => number {
  const resolve = agentResolver(graph);
  const kind = new Map(graph.nodes.map((n) => [n.id, n.kind]));
  const tools = new Map<string, Set<string>>();
  for (const e of graph.edges) {
    const k = kind.get(e.target);
    if (e.kind !== "call" || e.verdict === "deny") continue;
    if (k !== "tool" && k !== "mcp") continue;
    (tools.get(e.source) ?? tools.set(e.source, new Set()).get(e.source)!).add(
      e.target,
    );
  }
  return (agent) => {
    const r = resolve(agent);
    return r ? (tools.get(r.sourceId)?.size ?? 0) : 0;
  };
}
