/**
 * Tests for the /agents/:name hub — the agent-first page. It composes the
 * manifest (via ManifestView), the per-role policy (engine matrix) and the
 * 24h activity for one agent, and handles the unknown-agent case.
 */

import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";

import { useActive } from "@/lib/active";
import { AgentHub } from "@/routes/AgentHub";
import { renderWithProviders } from "@/test/render";

// Stub the heavy graph (ReactFlow) — the hub only needs it to mount with the
// agent's subgraph; PolicyFlow itself is tested in PolicyGraphDialog.test.
vi.mock("@/components/policy_files/PolicyGraphDialog", () => ({
  PolicyFlow: ({ graph }: { graph: { nodes: unknown[] } }) => (
    <div data-testid="agent-graph">{graph.nodes.length} nodes</div>
  ),
}));

const PROJECT = "p1";

const MANIFEST = {
  name: "support_bot",
  manifest: {
    name: "support_bot",
    description: "First-line support.",
    framework: "langchain",
    model: "claude-sonnet-5-5",
    system_prompt: null,
    tools: [
      {
        name: "refund",
        description: "Refund an order",
        input_schema: { type: "object", properties: {}, required: [] },
      },
    ],
    skills: null,
    guards: null,
  },
  version: 3,
  content_hash: "hash-1",
  updated_at: "2026-06-01T10:00:00Z",
};

function stubFetch(role: string, manifestError = false) {
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      switch (url.pathname) {
        case "/v1/orgs":
          return json([{ id: "org-1", slug: "acme", name: "Acme Inc", role }]);
        case "/v1/orgs/org-1/projects":
          return json([{ id: PROJECT, org_id: "org-1", name: "demo-project" }]);
        case `/v1/projects/${PROJECT}/agents/manifest`:
          return manifestError
            ? new Response("boom", { status: 500 })
            : json([MANIFEST]);
        case `/v1/projects/${PROJECT}/policy-files`:
          return json([
            {
              name: "policy.yaml",
              content: "",
              content_hash: "h",
              updated_at: "",
            },
          ]);
        case `/v1/projects/${PROJECT}/policy/matrix`:
          return json({
            roles: ["guest", "support"],
            tools: ["refund"],
            cells: {
              refund: {
                guest: { mode: "deny", constraint: null },
                support: { mode: "allow", constraint: "args.amount <= 50" },
              },
            },
            defaults: {
              guest: { mode: "deny", constraint: null },
              support: { mode: "deny", constraint: null },
            },
            aliased_default: null,
          });
        case `/v1/projects/${PROJECT}/policy/graph`:
          return json({
            roles: ["support", "guest"],
            nodes: [
              { id: "agent:support_bot", kind: "agent", label: "support_bot" },
              { id: "tool:refund", kind: "tool", label: "refund" },
              { id: "role:support", kind: "role", label: "support" },
            ],
            edges: [
              {
                source: "role:support",
                target: "agent:support_bot",
                kind: "admission",
                via: null,
                verdict: "allow",
                constraints: [],
                roles: ["support"],
              },
              {
                source: "agent:support_bot",
                target: "tool:refund",
                kind: "call",
                via: null,
                verdict: "allow",
                constraints: [],
                roles: [],
              },
            ],
          });
        case `/v1/projects/${PROJECT}/audit/summary`:
          return json({
            totals: { all: 15, allow: 10, deny: 4, needs_approval: 1 },
            by_agent: [
              {
                key: "support_bot",
                all: 15,
                allow: 10,
                deny: 4,
                needs_approval: 1,
              },
            ],
            by_role: [],
            by_tool: [],
            by_user: [],
          });
        default:
          return new Response("not found", { status: 404 });
      }
    },
  );
}

function renderHub(name: string, role = "admin", manifestError = false) {
  stubFetch(role, manifestError);
  return renderWithProviders(
    <Routes>
      <Route path="/agents/:name" element={<AgentHub />} />
    </Routes>,
    { initialRoute: `/agents/${name}` },
  );
}

describe("AgentHub", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders the agent header, framework and 24h activity", async () => {
    renderHub("support_bot");
    expect(
      await screen.findByRole("heading", { name: "support_bot" }),
    ).toBeInTheDocument();
    expect(screen.getByText("langchain")).toBeInTheDocument();
    // 24h activity.
    expect(screen.getByText("Calls · 24h")).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("shows the KPI strip and the agent's local graph", async () => {
    renderHub("support_bot");
    // KPI strip (the header no longer duplicates it).
    expect(await screen.findByText("Calls · 24h")).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
    // The subgraph (agent + its role + tool) reaches the graph.
    expect(await screen.findByTestId("agent-graph")).toHaveTextContent(
      "3 nodes",
    );
  });

  it("shows how each role treats the agent's tools (engine matrix)", async () => {
    renderHub("support_bot");
    expect(
      await screen.findByTitle("support: args.amount <= 50"),
    ).toHaveTextContent("support allow*");
    // The tool row and the guest chip render from the same matrix.
    expect(screen.getByText("refund")).toBeInTheDocument();
    expect(screen.getByText("guest")).toBeInTheDocument();
  });

  it("links Ban agent to the prefilled /bans route for an admin", async () => {
    renderHub("support_bot", "admin");
    const ban = await screen.findByRole("link", { name: /ban agent/i });
    expect(ban).toHaveAttribute("href", "/bans?ban_agent=support_bot");
  });

  it("shows a not-found state for an unknown agent", async () => {
    renderHub("ghost_bot");
    expect(await screen.findByText(/No agent named/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /back to agents/i }),
    ).toBeInTheDocument();
  });

  it("shows a load error, not a missing-agent state, when the fetch fails", async () => {
    renderHub("support_bot", "admin", true);
    expect(
      await screen.findByText(/Couldn't load this agent/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No agent named/)).not.toBeInTheDocument();
  });
});
