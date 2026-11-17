import { describe, expect, it } from "vitest";

import type { PolicyGraph } from "./api";
import { collapseTools, isolate } from "./graph-collapse";

const call = (source: string, target: string, verdict = "allow") => ({
  source,
  target,
  kind: "call" as const,
  via: null,
  verdict: verdict as "allow" | "deny",
  constraints: [],
  roles: [],
});

/** Agent a calls n tools; agent b calls the first two. */
function graph(n: number): PolicyGraph {
  const tools = Array.from({ length: n }, (_, i) => `tool:t${i}`);
  return {
    roles: [],
    nodes: [
      { id: "agent:a", kind: "agent", label: "a" },
      { id: "agent:b", kind: "agent", label: "b" },
      ...tools.map((id) => ({ id, kind: "tool" as const, label: id.slice(5) })),
    ],
    edges: [...tools.map((t) => call("agent:a", t)), call("agent:b", tools[0])],
  };
}

describe("collapseTools", () => {
  it("leaves a graph within the tool budget alone", () => {
    const g = graph(5);
    expect(collapseTools(g, new Set())).toBe(g);
  });

  it("folds an agent's tools past its share into one +N node", () => {
    const shown = collapseTools(graph(20), new Set(), 12, 6);
    const more = shown.nodes.find((n) => n.id === "more:agent:a");
    expect(more?.label).toBe("+14 tools");
    expect(shown.nodes.filter((n) => n.kind === "tool")).toHaveLength(6);
    // One edge into the fold, with the hidden calls' single verdict.
    const into = shown.edges.filter((e) => e.target === "more:agent:a");
    expect(into).toHaveLength(1);
    expect(into[0].verdict).toBe("allow");
  });

  it("marks a fold of mixed verdicts as mixed", () => {
    const g = graph(20);
    g.edges[10] = { ...g.edges[10], verdict: "deny" };
    const shown = collapseTools(g, new Set(), 12, 6);
    expect(shown.edges.find((e) => e.target === "more:agent:a")?.verdict).toBe(
      "mixed",
    );
  });

  it("keeps every tool of an expanded agent", () => {
    const shown = collapseTools(graph(20), new Set(["agent:a"]), 12, 6);
    expect(shown.nodes.some((n) => n.kind === "more")).toBe(false);
    expect(shown.nodes.filter((n) => n.kind === "tool")).toHaveLength(20);
  });
});

describe("isolate", () => {
  it("keeps only the nodes on a path through the focus", () => {
    const g = graph(3);
    const shown = isolate(g, "agent:b");
    expect(shown.nodes.map((n) => n.id).sort()).toEqual(["agent:b", "tool:t0"]);
  });
});
