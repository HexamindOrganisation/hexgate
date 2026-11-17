/**
 * The /agents filter: what each agent is (status, framework, 24h activity,
 * delegation, which roles run it) and which agents a filter keeps. Options
 * within a section widen the match; sections narrow it. Pure.
 */

import type { PolicyGraph } from "./api";
import { agentSubgraph } from "./agent-graph";

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
  /** 24h counts; null while the summary hasn't loaded. */
  activity: { all: number; deny: number; needs_approval: number } | null;
  reaches: boolean;
  calledBy: boolean;
  /** Roles that run it; null when no role gates running it. */
  runBy: Set<string> | null;
};

/** The roles that run `name`, from the project graph's admission edges;
 * null when its graph has none (running it isn't gated by role). */
export function rolesThatRun(
  graph: PolicyGraph | undefined,
  name: string,
): Set<string> | null {
  if (!graph) return null;
  const sub = agentSubgraph(graph, name);
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

const ACTIVITY: Record<Activity, (a: AgentFacts["activity"]) => boolean> = {
  deny: (a) => !!a && a.deny > 0,
  approval: (a) => !!a && a.needs_approval > 0,
  quiet: (a) => !!a && a.all === 0,
};

/** Whether `a` passes `f`. */
export function matches(f: AgentFilters, a: AgentFacts): boolean {
  if (f.status === "registered" && !a.registered) return false;
  if (f.status === "unregistered" && a.registered) return false;
  if (f.activity.length && !f.activity.some((k) => ACTIVITY[k](a.activity)))
    return false;
  if (f.frameworks.length && !f.frameworks.includes(a.framework ?? ""))
    return false;
  if (
    f.delegation.length &&
    !f.delegation.some((d) => (d === "reaches" ? a.reaches : a.calledBy))
  )
    return false;
  if (f.roles.length && a.runBy && !f.roles.some((r) => a.runBy!.has(r)))
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
