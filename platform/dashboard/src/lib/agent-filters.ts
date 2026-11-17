/**
 * The /agents filter: what each agent is (status, framework, 24h activity,
 * delegation, which roles run it) and which agents a filter keeps. Options
 * within a section widen the match; sections narrow it. Pure.
 */

import type { PolicyGraph } from "./api";
import { agentResolver } from "./agent-graph";
import { runnersFrom } from "./agent-graph-view";
import { push } from "./graph-layout";

export type Activity = "deny" | "approval" | "quiet";
export type Delegation = "reaches" | "called";

export type AgentFilters = {
  status: "any" | "registered" | "unregistered";
  activity: Activity[];
  frameworks: string[];
  delegation: Delegation[];
  /** Agents any of these roles can run. */
  roles: string[];
};

export const NO_FILTERS: AgentFilters = {
  status: "any",
  activity: [],
  frameworks: [],
  delegation: [],
  roles: [],
};

/** What the filter reads off one agent. */
export type AgentFacts = {
  name: string;
  registered: boolean;
  framework: string | null;
  /** 24h counts; null while the summary hasn't loaded (or failed): then the
   * activity filters don't apply, rather than hide every agent. */
  activity: { all: number; deny: number; needs_approval: number } | null;
  reaches: boolean;
  calledBy: boolean;
  /** The policy graph loaded: without it delegation and roles are unknown,
   * so those filters don't apply. */
  graphKnown: boolean;
  /** Roles that run it; null when no role gates running it, or the graph
   * isn't loaded: then the role filter keeps it. */
  runBy: Set<string> | null;
};

/** The roles that run each of `names`, from the project graph's admission
 * edges, resolved as agentSubgraph resolves an agent (its own node when the
 * policy declares it, else `*`'s); null for one no role gates. One pass over
 * the edges, however many agents. */
export function rolesThatRun(
  graph: PolicyGraph | undefined,
  names: string[],
): Map<string, Set<string> | null> {
  const out = new Map<string, Set<string> | null>();
  if (!graph) {
    for (const n of names) out.set(n, null);
    return out;
  }
  const admits = new Map<string, PolicyGraph["edges"]>();
  for (const e of graph.edges)
    if (e.kind === "admission") push(admits, e.target, e);
  // With admissions anywhere, running is role-gated: an agent the policy
  // can't place at all (no node, no `*`) is run by no role.
  const gated = admits.size > 0;
  const resolve = agentResolver(graph);
  for (const name of names) {
    const r = resolve(name);
    if (!r) {
      out.set(name, gated ? new Set() : null);
      continue;
    }
    out.set(
      name,
      runnersFrom([...r.admitIds].flatMap((id) => admits.get(id) ?? [])),
    );
  }
  return out;
}

export const ACTIVITY_LABEL: Record<Activity, string> = {
  deny: "Has denials",
  approval: "Waiting on approval",
  quiet: "No calls",
};

const ACTIVITY: Record<Activity, (a: AgentFacts["activity"]) => boolean> = {
  deny: (a) => !!a && a.deny > 0,
  approval: (a) => !!a && a.needs_approval > 0,
  quiet: (a) => !!a && a.all === 0,
};

/** Whether `a` passes `f`. */
export function matches(f: AgentFilters, a: AgentFacts): boolean {
  if (f.status === "registered" && !a.registered) return false;
  if (f.status === "unregistered" && a.registered) return false;
  if (
    f.activity.length &&
    a.activity &&
    !f.activity.some((k) => ACTIVITY[k](a.activity))
  )
    return false;
  if (f.frameworks.length && !f.frameworks.includes(a.framework ?? ""))
    return false;
  if (
    f.delegation.length &&
    a.graphKnown &&
    !f.delegation.some((d) => (d === "reaches" ? a.reaches : a.calledBy))
  )
    return false;
  if (
    f.roles.length &&
    a.graphKnown &&
    a.runBy &&
    !f.roles.some((r) => a.runBy!.has(r))
  )
    return false;
  return true;
}

/** How many filters are set, for the Filters button's badge. */
export function activeCount(f: AgentFilters): number {
  return (
    (f.status === "any" ? 0 : 1) +
    f.activity.length +
    f.frameworks.length +
    f.delegation.length +
    f.roles.length
  );
}

/** One option's count: the agents it alone would keep. */
export function countWhere(
  facts: AgentFacts[],
  patch: Partial<AgentFilters>,
): number {
  const f = { ...NO_FILTERS, ...patch };
  return facts.filter((a) => matches(f, a)).length;
}
