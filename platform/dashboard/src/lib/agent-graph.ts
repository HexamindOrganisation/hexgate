import type { PolicyGraph, PolicyGraphEdge } from "@/lib/api";

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
  const edges = new Set(
    graph.edges.filter((e) => e.source === node.id || e.target === node.id),
  );
  const keep = new Set<string>([node.id]);
  for (const e of edges) {
    keep.add(e.source);
    keep.add(e.target);
  }
  // Agents THIS one reaches (its outbound reach targets) keep their own
  // outgoing edges, so you see what a handoff can touch — but a caller that
  // reaches INTO this agent does not drag its own tools in.
  const kindById = new Map(graph.nodes.map((n) => [n.id, n.kind]));
  const reaches = [...edges]
    .filter((e) => e.source === node.id && kindById.get(e.target) === "agent")
    .map((e) => e.target);
  for (const sub of reaches) {
    for (const e of graph.edges) {
      if (e.source === sub) {
        edges.add(e);
        keep.add(e.target);
      }
    }
  }
  return {
    nodes: graph.nodes.filter((n) => keep.has(n.id)),
    edges: [...edges],
    roles: graph.roles,
  };
}

/** One agent's delegation neighbours in the project graph: the agents it
 * reaches (agent-as-tool, outbound) and the agents that reach it (inbound,
 * i.e. it is their sub-agent). Returns deduped labels in graph order; empty
 * when the agent isn't in the graph. Edges are classed by endpoint kind, not
 * edge kind, so an agent-as-tool reach lowered to a call still counts. */
export function agentReach(
  graph: PolicyGraph,
  agentName: string,
): { reaches: string[]; calledBy: string[] } {
  const { self, agentLabel } = agentNodes(graph, agentName);
  if (!self) return { reaches: [], calledBy: [] };
  const reaches = new Set<string>();
  const calledBy = new Set<string>();
  for (const e of graph.edges) {
    if (e.source === self.id && agentLabel.has(e.target)) {
      reaches.add(agentLabel.get(e.target)!);
    }
    if (e.target === self.id && agentLabel.has(e.source)) {
      calledBy.add(agentLabel.get(e.source)!);
    }
  }
  return { reaches: [...reaches], calledBy: [...calledBy] };
}

/** The agent's outbound reach edges, keyed `<target>:<via>` (the graph merges
 * one edge per pair across roles). Edges with no via (a reach lowered to a
 * call) can't be matched, so are skipped. */
export function agentReachEdges(
  graph: PolicyGraph,
  agentName: string,
): Map<string, PolicyGraphEdge> {
  const { self, agentLabel } = agentNodes(graph, agentName);
  const out = new Map<string, PolicyGraphEdge>();
  if (!self) return out;
  for (const e of graph.edges) {
    if (e.source === self.id && agentLabel.has(e.target) && e.via) {
      out.set(`${agentLabel.get(e.target)}:${e.via}`, e);
    }
  }
  return out;
}

function agentNodes(graph: PolicyGraph, agentName: string) {
  const self = graph.nodes.find(
    (n) => n.kind === "agent" && n.label === agentName,
  );
  const agentLabel = new Map(
    graph.nodes.filter((n) => n.kind === "agent").map((n) => [n.id, n.label]),
  );
  return { self, agentLabel };
}
