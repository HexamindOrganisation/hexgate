import { describe, expect, it } from "vitest";

import type { PolicyGraph } from "./api";
import {
  type AgentFacts,
  NO_FILTERS,
  matches,
  rolesThatRun,
} from "./agent-filters";

const facts = (over: Partial<AgentFacts> = {}): AgentFacts => ({
  name: "a",
  registered: true,
  framework: "langchain",
  activity: { all: 3, deny: 1, needs_approval: 0 },
  reaches: false,
  calledBy: false,
  runBy: new Set(["ops"]),
  graphKnown: true,
  ...over,
});

describe("matches", () => {
  it("ORs options within a section and ANDs sections", () => {
    const f = { ...NO_FILTERS, activity: ["deny" as const, "quiet" as const] };
    expect(matches(f, facts())).toBe(true);
    expect(matches({ ...f, frameworks: ["openai"] }, facts())).toBe(false);
  });

  it("doesn't hide agents whose activity or roles aren't known", () => {
    const f = { ...NO_FILTERS, activity: ["deny" as const], roles: ["guest"] };
    expect(matches(f, facts({ activity: null, runBy: null }))).toBe(true);
  });

  it("doesn't apply delegation or roles without the graph", () => {
    const f = { ...NO_FILTERS, delegation: ["reaches" as const], roles: ["x"] };
    expect(matches(f, facts({ graphKnown: false }))).toBe(true);
    expect(matches(f, facts())).toBe(false);
  });

  it("keeps an agent only if a picked role runs it", () => {
    expect(matches({ ...NO_FILTERS, roles: ["guest"] }, facts())).toBe(false);
    expect(matches({ ...NO_FILTERS, roles: ["ops"] }, facts())).toBe(true);
  });
});

describe("rolesThatRun", () => {
  const admit = (role: string, agent: string, verdict = "allow") => ({
    source: `role:${role}`,
    target: `agent:${agent}`,
    kind: "admission" as const,
    via: null,
    verdict: verdict as "allow" | "deny",
    constraints: [],
    roles: [],
  });
  const g: PolicyGraph = {
    roles: ["ops", "guest"],
    nodes: [
      { id: "agent:*", kind: "agent", label: "*" },
      { id: "agent:a", kind: "agent", label: "a" },
      { id: "role:ops", kind: "role", label: "ops" },
      { id: "role:guest", kind: "role", label: "guest" },
    ],
    edges: [
      admit("ops", "a"),
      admit("guest", "a", "deny"),
      admit("guest", "*"),
    ],
  };

  it("reads each agent's runners in one pass, through * when undeclared", () => {
    const m = rolesThatRun(g, ["a", "b"]);
    expect([...(m.get("a") ?? [])]).toEqual(["ops"]);
    // b isn't declared: it runs as *, which guest runs.
    expect([...(m.get("b") ?? [])]).toEqual(["guest"]);
  });

  it("is unknown for every agent without a graph", () => {
    expect(rolesThatRun(undefined, ["a"]).get("a")).toBeNull();
  });
});
