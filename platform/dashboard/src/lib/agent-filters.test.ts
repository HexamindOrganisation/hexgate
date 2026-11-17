import { describe, expect, it } from "vitest";

import { type AgentFacts, NO_FILTERS, matches } from "./agent-filters";

const facts = (over: Partial<AgentFacts> = {}): AgentFacts => ({
  name: "a",
  registered: true,
  framework: "langchain",
  activity: { all: 3, deny: 1, needs_approval: 0 },
  reaches: false,
  calledBy: false,
  runBy: new Set(["ops"]),
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

  it("keeps an agent only if a picked role runs it", () => {
    expect(matches({ ...NO_FILTERS, roles: ["guest"] }, facts())).toBe(false);
    expect(matches({ ...NO_FILTERS, roles: ["ops"] }, facts())).toBe(true);
  });
});
