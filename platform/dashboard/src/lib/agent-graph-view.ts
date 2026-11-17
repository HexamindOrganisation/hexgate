/**
 * Keeps one agent's graph readable however big the agent: past a few tools
 * they're drawn as one node per verdict (the names go in a list beside the
 * graph), and past a few roles or sub-agents the rest are a "+N more" node.
 * Pure, so it's testable without React Flow.
 */

import type { PolicyGraph, PolicyGraphEdge, PolicyGraphNode } from "./api";

/** Past this many tools, they're grouped by verdict. */
export const MAX_TOOLS = 8;
/** Past this many roles or sub-agents, the rest fold into "+N more". */
export const MAX_SIDE = 5;

export type Verdict = PolicyGraphEdge["verdict"];
/** A tool group, a "+N roles" node, or a "+N sub-agents" node. */
export type ExtraNode = {
  id: string;
  kind: "group" | "more-roles" | "more-agents";
  label: string;
  /** A second line ("9 with limits"). */
  sub?: string;
  verdict?: Verdict;
};
export type ShownNode = (PolicyGraphNode & { sub?: string }) | ExtraNode;
export type ShownEdge = Omit<PolicyGraphEdge, "verdict"> & {
  /** "mixed" on the edge into "+N sub-agents" whose reaches differ. */
  verdict: Verdict | "mixed";
};
export type ShownGraph = {
  nodes: ShownNode[];
  edges: ShownEdge[];
  roles: string[];
};

export const GROUP_PREFIX = "group:";
export const MORE_ROLES = "more:roles";
export const MORE_AGENTS = "more:agents";

const ORDER: Verdict[] = ["allow", "approval_required", "deny"];
const GROUP_LABEL: Record<Verdict, string> = {
  allow: "allowed",
  approval_required: "need approval",
  deny: "denied",
};
const isTool = (kind: string) => kind === "tool" || kind === "mcp";
/** Allowed first, denied last: what's cut is the least that can run. */
const rank = (e: { verdict: string }) =>
  e.verdict === "deny" ? 2 : e.verdict === "approval_required" ? 1 : 0;

/** `sub` (from agentSubgraph, the agent first) as drawn: tools grouped by
 * verdict past `maxTools`, roles and sub-agents past `maxSide` folded into
 * "+N more". `toolCount` captions each sub-agent. Also returns each group's
 * tool edges, for the list beside the graph. */
export function compactAgentGraph(
  sub: PolicyGraph,
  toolCount: (agent: string) => number = () => 0,
  maxTools = MAX_TOOLS,
  maxSide = MAX_SIDE,
): { graph: ShownGraph; groups: Map<Verdict, PolicyGraphEdge[]> } {
  const self = sub.nodes[0];
  const kind = new Map(sub.nodes.map((n) => [n.id, n.kind]));
  const groups = new Map<Verdict, PolicyGraphEdge[]>();
  if (!self) return { graph: sub, groups };

  const calls = sub.edges.filter(
    (e) => e.source === self.id && isTool(kind.get(e.target) ?? ""),
  );
  const grouped = new Set(calls.length > maxTools ? calls : []);
  for (const e of grouped) {
    const g = groups.get(e.verdict) ?? [];
    g.push(e);
    groups.set(e.verdict, g);
  }

  // Roles in and sub-agents out, permitted first, so a cut hides the least.
  const roleEdges = sub.edges
    .filter((e) => e.kind === "admission" && e.target === self.id)
    .sort((a, b) => rank(a) - rank(b));
  const agentEdges = sub.edges
    .filter((e) => e.kind === "reach" && e.source === self.id)
    .sort((a, b) => rank(a) - rank(b));
  const cutRoles = new Set(roleEdges.slice(maxSide));
  const cutAgents = new Set(agentEdges.slice(maxSide));

  const edges: ShownEdge[] = sub.edges.filter(
    (e) => !grouped.has(e) && !cutRoles.has(e) && !cutAgents.has(e),
  );
  const used = new Set(edges.flatMap((e) => [e.source, e.target]));
  used.add(self.id);
  const nodes: ShownNode[] = sub.nodes
    .filter((n) => used.has(n.id))
    .map((n) =>
      n.kind === "agent" && n.id !== self.id
        ? { ...n, sub: `${toolCount(n.label)} tools` }
        : n,
    );

  for (const v of ORDER) {
    const g = groups.get(v);
    if (!g) continue;
    const id = `${GROUP_PREFIX}${v}`;
    const limited = g.filter((e) => e.constraints.length > 0).length;
    nodes.push({
      id,
      kind: "group",
      label: `${g.length} ${GROUP_LABEL[v]}`,
      sub: v !== "deny" && limited ? `${limited} with limits` : undefined,
      verdict: v,
    });
    edges.push({
      source: self.id,
      target: id,
      kind: "call",
      via: null,
      verdict: v,
      constraints: [],
      roles: [],
    });
  }
  if (cutRoles.size) {
    nodes.push({
      id: MORE_ROLES,
      kind: "more-roles",
      label: `+${cutRoles.size} more roles`,
    });
  }
  if (cutAgents.size) {
    const verdicts = new Set([...cutAgents].map((e) => e.verdict));
    nodes.push({
      id: MORE_AGENTS,
      kind: "more-agents",
      label: `+${cutAgents.size} more`,
    });
    edges.push({
      source: self.id,
      target: MORE_AGENTS,
      kind: "reach",
      via: null,
      verdict: verdicts.size === 1 ? [...verdicts][0] : "mixed",
      constraints: [],
      roles: [],
    });
  }
  return { graph: { nodes, edges, roles: sub.roles }, groups };
}

/** Two grants of one verdict as one: either may apply, so an unconditional
 * one wins and two conditions become alternatives. */
function eitherOf(a: string[], b: string[]): string[] {
  if (!a.length || !b.length) return [];
  const all = (c: string[]) => (c.length > 1 ? `(${c.join(" ∧ ")})` : c[0]);
  return a.join() === b.join() ? a : [`${all(a)} ∨ ${all(b)}`];
}

/** Graphs for several roles as one caller holding them all: the engine's
 * permissive union per edge, allow over approval over deny (decision.py
 * `combine_role_verdicts`). Equal verdicts keep either one's conditions. */
export function mergeRoleGraphs(graphs: PolicyGraph[]): PolicyGraph {
  if (graphs.length === 1) return graphs[0];
  const nodes = new Map<string, PolicyGraphNode>();
  const edges = new Map<string, PolicyGraphEdge>();
  for (const g of graphs) {
    for (const n of g.nodes) nodes.set(n.id, n);
    for (const e of g.edges) {
      const key = `${e.source}>${e.target}:${e.kind}:${e.via ?? ""}`;
      const had = edges.get(key);
      if (!had) {
        edges.set(key, e);
        continue;
      }
      const roles = [...new Set([...had.roles, ...e.roles])];
      const win = rank(e) < rank(had) ? e : had;
      const constraints =
        rank(e) === rank(had)
          ? eitherOf(had.constraints, e.constraints)
          : win.constraints;
      edges.set(key, { ...win, roles, constraints });
    }
  }
  return {
    nodes: [...nodes.values()],
    edges: [...edges.values()],
    roles: graphs[0]?.roles ?? [],
  };
}

/** A role in the picker: whether it runs the agent (null when the policy has
 * no admission block, so roles don't gate running it) and how many of its
 * tools that role may call. */
export type RoleOption = { name: string; runs: boolean | null; tools: number };

/** Every role in the project graph, as the role picker lists them for one
 * agent. `sub` is that agent's graph from the unfiltered project graph. */
export function roleOptionsFor(
  roles: string[],
  sub: PolicyGraph,
): RoleOption[] {
  const self = sub.nodes[0];
  const kind = new Map(sub.nodes.map((n) => [n.id, n.kind]));
  const admits = sub.edges.filter(
    (e) => e.kind === "admission" && e.target === self?.id,
  );
  const calls = sub.edges.filter(
    (e) =>
      e.source === self?.id &&
      e.verdict !== "deny" &&
      isTool(kind.get(e.target) ?? ""),
  );
  return roles.map((name) => ({
    name,
    runs: admits.length
      ? admits.some((e) => e.source === `role:${name}` && e.verdict !== "deny")
      : null,
    // An edge listing no roles holds for every role.
    tools: new Set(
      calls
        .filter((e) => !e.roles.length || e.roles.includes(name))
        .map((e) => e.target),
    ).size,
  }));
}
