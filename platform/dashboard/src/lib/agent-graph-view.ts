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

  // Roles in and sub-agents out, permitted first, so a cut hides the least;
  // counted by node, so two edges to one sub-agent are one slot.
  const cutBy = (edges: PolicyGraphEdge[], end: "source" | "target") => {
    const ranked = [...edges].sort((a, b) => rank(a) - rank(b));
    const keep = new Set<string>();
    for (const e of ranked)
      if (keep.size < maxSide || keep.has(e[end])) keep.add(e[end]);
    return new Set(ranked.filter((e) => !keep.has(e[end])));
  };
  const cutRoles = cutBy(
    sub.edges.filter((e) => e.kind === "admission" && e.target === self.id),
    "source",
  );
  const cutAgents = cutBy(
    sub.edges.filter((e) => e.kind === "reach" && e.source === self.id),
    "target",
  );
  const count = (es: Set<PolicyGraphEdge>, end: "source" | "target") =>
    new Set([...es].map((e) => e[end])).size;
  /** One edge for a "+N" node: the folded edges' verdict, or "mixed". */
  const foldEdge = (
    es: Set<PolicyGraphEdge>,
    source: string,
    target: string,
    kind: PolicyGraphEdge["kind"],
  ): ShownEdge => {
    const verdicts = new Set([...es].map((e) => e.verdict));
    return {
      source,
      target,
      kind,
      via: null,
      verdict: verdicts.size === 1 ? [...verdicts][0] : "mixed",
      constraints: [],
      roles: [],
    };
  };

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
      label: `+${count(cutRoles, "source")} more roles`,
    });
    edges.push(foldEdge(cutRoles, MORE_ROLES, self.id, "admission"));
  }
  if (cutAgents.size) {
    nodes.push({
      id: MORE_AGENTS,
      kind: "more-agents",
      label: `+${count(cutAgents, "target")} more`,
    });
    edges.push(foldEdge(cutAgents, self.id, MORE_AGENTS, "reach"));
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

const FALLBACK: Record<Verdict, string> = {
  allow: "allowed",
  approval_required: "needs approval",
  deny: "denied",
};

/** Graphs for several roles as one caller holding them all: the engine's
 * permissive union per call, allow over approval over deny (decision.py
 * `combine_role_verdicts`). An edge shows its most permissive verdict; when
 * that one is conditional and a lesser grant still applies outside it, the
 * constraints say so ("otherwise needs approval"). */
export function mergeRoleGraphs(graphs: PolicyGraph[]): PolicyGraph {
  if (graphs.length === 1) return graphs[0];
  const nodes = new Map<string, PolicyGraphNode>();
  const grants = new Map<string, PolicyGraphEdge[]>();
  for (const g of graphs) {
    for (const n of g.nodes) nodes.set(n.id, n);
    for (const e of g.edges) {
      const key = `${e.source}>${e.target}:${e.kind}:${e.via ?? ""}`;
      grants.set(key, [...(grants.get(key) ?? []), e]);
    }
  }
  const edges = [...grants.values()].map((es) => {
    const byRank = [0, 1, 2].map((r) => es.filter((e) => rank(e) === r));
    const tiers = byRank
      .filter((t) => t.length)
      .map((t) => ({
        edge: t[0],
        constraints: t
          .slice(1)
          .reduce((c, e) => eitherOf(c, e.constraints), t[0].constraints),
      }));
    const [win, ...rest] = tiers;
    // Outside the winner's condition, the next grant that isn't a deny.
    const next = win.constraints.length
      ? rest.find((t) => t.edge.verdict !== "deny")
      : undefined;
    const constraints = next
      ? [
          ...win.constraints,
          `otherwise ${FALLBACK[next.edge.verdict]}${
            next.constraints.length ? ` if ${next.constraints.join(" ∧ ")}` : ""
          }`,
        ]
      : win.constraints;
    return {
      ...win.edge,
      constraints,
      roles: [...new Set(es.flatMap((e) => e.roles))],
    };
  });
  return {
    nodes: [...nodes.values()],
    edges,
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
  const runners = runnersOf(sub);
  const calls = sub.edges.filter(
    (e) =>
      e.source === self?.id &&
      e.verdict !== "deny" &&
      isTool(kind.get(e.target) ?? ""),
  );
  return roles.map((name) => ({
    name,
    runs: runners ? runners.has(name) : null,
    // An edge listing no roles holds for every role.
    tools: new Set(
      calls
        .filter((e) => !e.roles.length || e.roles.includes(name))
        .map((e) => e.target),
    ).size,
  }));
}

/** The roles that run the agent of `sub` (its graph, the agent first), from
 * its admission edges; null when it has none (running it isn't gated). */
export function runnersOf(sub: PolicyGraph): Set<string> | null {
  const self = sub.nodes[0]?.id;
  const admits = sub.edges.filter(
    (e) => e.kind === "admission" && e.target === self,
  );
  if (!admits.length) return null;
  return new Set(
    admits
      .filter((e) => e.verdict !== "deny")
      .map((e) => e.source.slice("role:".length)),
  );
}
