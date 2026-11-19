/**
 * Keeps one agent's graph readable however big the agent. The canvas only
 * draws real nodes and edges: past a few tools they leave the canvas for a
 * summary by verdict (with a list), and past a few roles or sub-agents the
 * rest are counted, not drawn. Pure, so it's testable without React Flow.
 */

import { ApiError, type PolicyGraph, type PolicyGraphEdge } from "./api";
import { agentResolver } from "./agent-graph";
import { push } from "./graph-layout";

/** Past this many tools, they're summarised by verdict instead of drawn. */
export const MAX_TOOLS = 8;
/** Past this many roles or sub-agents, the rest are counted, not drawn. */
export const MAX_SIDE = 5;

export type Verdict = PolicyGraphEdge["verdict"];

const isTool = (kind: string) => kind === "tool" || kind === "mcp";
/** Allowed first, denied last: what's left out is the least that can run. */
const rank = (e: { verdict: string }) =>
  e.verdict === "deny" ? 2 : e.verdict === "approval_required" ? 1 : 0;

export type AgentView = {
  /** What the canvas draws: real nodes and edges only. */
  graph: PolicyGraph;
  /** Tools by verdict, when there are too many to draw; else empty. */
  tools: Map<Verdict, PolicyGraphEdge[]>;
  /** Roles and sub-agents left off the canvas. */
  hiddenRoles: number;
  hiddenAgents: number;
  /** Every reach to a sub-agent, for the list beside the graph. */
  reaches: PolicyGraphEdge[];
};

/** `sub` (from agentSubgraph, the agent first) as the canvas draws it. */
export function compactAgentGraph(
  sub: PolicyGraph,
  maxTools = MAX_TOOLS,
  maxSide = MAX_SIDE,
): AgentView {
  const self = sub.nodes[0];
  const tools = new Map<Verdict, PolicyGraphEdge[]>();
  if (!self)
    return { graph: sub, tools, hiddenRoles: 0, hiddenAgents: 0, reaches: [] };
  const kind = new Map(sub.nodes.map((n) => [n.id, n.kind]));

  const calls = sub.edges.filter(
    (e) => e.source === self.id && isTool(kind.get(e.target) ?? ""),
  );
  const offTools = new Set(calls.length > maxTools ? calls : []);
  for (const e of offTools) push(tools, e.verdict, e);

  // Kept by node, permitted first: two edges to one sub-agent are one slot.
  const keepFirst = (edges: PolicyGraphEdge[], end: "source" | "target") => {
    const keep = new Set<string>();
    for (const e of [...edges].sort((a, b) => rank(a) - rank(b)))
      if (keep.size < maxSide) keep.add(e[end]);
    const all = new Set(edges.map((e) => e[end]));
    return { keep, hidden: all.size - keep.size };
  };
  const roles = keepFirst(
    sub.edges.filter((e) => e.kind === "admission" && e.target === self.id),
    "source",
  );
  const reaches = sub.edges.filter(
    (e) => e.kind === "reach" && e.source === self.id,
  );
  const agents = keepFirst(reaches, "target");

  const edges = sub.edges.filter(
    (e) =>
      !offTools.has(e) &&
      !(e.kind === "admission" && !roles.keep.has(e.source)) &&
      !(e.kind === "reach" && !agents.keep.has(e.target)),
  );
  const used = new Set(edges.flatMap((e) => [e.source, e.target]));
  used.add(self.id);
  return {
    graph: {
      nodes: sub.nodes.filter((n) => used.has(n.id)),
      edges,
      roles: sub.roles,
    },
    tools,
    hiddenRoles: roles.hidden,
    hiddenAgents: agents.hidden,
    reaches,
  };
}

/** The roles that run each of `names`, as the engine admits them: when the
 * policy gates running agents by role anywhere (it has admission edges),
 * every agent is gated, so one with no admission of its own, or none the
 * policy can place, is run by no role. With no admission anywhere, null:
 * running isn't gated. Agents resolve as agentSubgraph resolves them. The one
 * copy of this rule: the hub's role picker and the /agents filter both read
 * it. */
export function rolesThatRun(
  graph: PolicyGraph | undefined,
  names: string[],
): Map<string, Set<string> | null> {
  const out = new Map<string, Set<string> | null>();
  const admits = new Map<string, PolicyGraphEdge[]>();
  for (const e of graph?.edges ?? [])
    if (e.kind === "admission") push(admits, e.target, e);
  if (!graph || admits.size === 0) {
    for (const n of names) out.set(n, null);
    return out;
  }
  const resolve = agentResolver(graph);
  for (const name of names) {
    const ids = resolve(name)?.admitIds ?? new Set<string>();
    out.set(
      name,
      new Set(
        [...ids]
          .flatMap((id) => admits.get(id) ?? [])
          .filter((e) => e.verdict !== "deny")
          .map((e) => e.source.slice("role:".length)),
      ),
    );
  }
  return out;
}

/** A role in the picker, and whether it runs the agent (null when no role
 * gates running it). */
export type RoleOption = { name: string; runs: boolean | null };

/** Every role in the project, as the role picker lists them for one agent,
 * given the roles that run it (rolesThatRun). Every role, because which ones
 * change what it may do isn't in the graph (#486). */
export function roleOptionsFor(
  roles: string[],
  runners: Set<string> | null,
): RoleOption[] {
  return roles.map((name) => ({
    name,
    runs: runners ? runners.has(name) : null,
  }));
}

/** Why a graph fetch failed: 422 the modules don't compose, 404 the project
 * isn't visible any more, else it didn't load. */
export type GraphError = "compose" | "gone" | "load";

export function graphErrorOf(error: unknown): GraphError {
  const status = error instanceof ApiError ? error.status : null;
  return status === 422 ? "compose" : status === 404 ? "gone" : "load";
}

/** What to tell the user about a failed graph. */
export const GRAPH_ERROR_TEXT: Record<GraphError, string> = {
  compose:
    "The policy modules don't compose, so there's no graph: fix the lints in Policies.",
  gone: "You don't have access to this project's policy any more.",
  load: "Couldn't load the graph. Refresh to retry.",
};
