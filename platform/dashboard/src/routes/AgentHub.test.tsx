/**
 * Tests for the /agents/:name hub — the agent-first page. It composes the
 * manifest (via ManifestView), the per-role policy (engine matrix) and the
 * 24h activity for one agent, and handles the unknown-agent case.
 */

import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";

import { useActive } from "@/lib/active";
import {
  AgentDecisionsTab,
  AgentHub,
  AgentOverviewTab,
  AgentPoliciesTab,
  AgentResourcesTab,
} from "@/routes/AgentHub";
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

function stubFetch(
  role: string,
  manifestError = false,
  summaryError = false,
  noAgentInGraph = false,
  matrixError = false,
) {
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
          if (matrixError) return new Response("boom", { status: 500 });
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
          if (noAgentInGraph)
            return json({
              roles: [],
              nodes: [{ id: "agent:other", kind: "agent", label: "other" }],
              edges: [],
            });
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
        case `/v1/projects/${PROJECT}/audit/decisions`:
          return json({
            rows: [
              {
                event_id: "evt-1",
                occurred_at: "2026-06-01T12:00:00Z",
                received_at: "2026-06-01T12:00:01Z",
                agent_name: "support_bot",
                agent_version_id: "av-1",
                session_id: "sess-1",
                user_id: "u-1",
                tool_name: "refund",
                user_roles: ["guest"],
                deciding_role: "",
                outcome: "deny",
                error_type: "",
                reason: "Guests can't refund.",
                violations: ["refund: role guest denied"],
                hint: null,
                arguments: null,
                attributes: null,
                run_id: null,
              },
            ],
            total: 1,
            limit: 50,
            offset: 0,
          });
        case `/v1/projects/${PROJECT}/audit/summary`:
          if (summaryError) return new Response("boom", { status: 500 });
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

function renderHub(
  name: string,
  {
    role = "admin",
    manifestError = false,
    summaryError = false,
    noAgentInGraph = false,
    matrixError = false,
    tab = "",
  }: {
    role?: string;
    manifestError?: boolean;
    summaryError?: boolean;
    noAgentInGraph?: boolean;
    matrixError?: boolean;
    tab?: "" | "/resources" | "/policies" | "/decisions";
  } = {},
) {
  stubFetch(role, manifestError, summaryError, noAgentInGraph, matrixError);
  return renderWithProviders(
    <Routes>
      <Route path="/agents/:name" element={<AgentHub />}>
        <Route index element={<AgentOverviewTab />} />
        <Route path="resources" element={<AgentResourcesTab />} />
        <Route path="policies" element={<AgentPoliciesTab />} />
        <Route path="decisions" element={<AgentDecisionsTab />} />
      </Route>
    </Routes>,
    { initialRoute: `/agents/${name}${tab}` },
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

  it("navigates from the Overview graph to the Resources tab", async () => {
    renderHub("support_bot");
    expect(await screen.findByTestId("agent-graph")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("link", { name: /Resources/ }));
    // The manifest's tools live on Resources now, not the Overview.
    expect(await screen.findByText("refund")).toBeInTheDocument();
  });

  it("shows em-dashes, not fake zeros, when 24h activity fails to load", async () => {
    renderHub("support_bot", { summaryError: true });
    expect(
      await screen.findByText(/Couldn't load the last 24 hours/),
    ).toBeInTheDocument();
    // Calls / Denied / Approvals all unknown (not fake zeros).
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("says the agent has nothing mapped when it isn't in the graph", async () => {
    renderHub("support_bot", { noAgentInGraph: true });
    expect(
      await screen.findByText(/No roles, tools or reach mapped/),
    ).toBeInTheDocument();
  });

  it("shows how each role treats the agent's tools (engine matrix)", async () => {
    renderHub("support_bot", { tab: "/resources" });
    expect(
      await screen.findByTitle("support: args.amount <= 50"),
    ).toHaveTextContent("support allow*");
    // The tool row and the guest chip render from the same matrix.
    expect(screen.getByText("refund")).toBeInTheDocument();
    expect(screen.getByText("guest")).toBeInTheDocument();
  });

  it("shows the resolved roles x tools grid on the Policies tab", async () => {
    renderHub("support_bot", { tab: "/policies" });
    expect(
      await screen.findByText(/How each role may use/),
    ).toBeInTheDocument();
    // The engine grid cell carries the constraint as its title + an allow mode
    // (awaited — the matrix loads after the static header).
    expect(await screen.findByTitle("args.amount <= 50")).toHaveTextContent(
      "allow",
    );
    // The governing file is listed.
    expect(screen.getByText("policy.yaml")).toBeInTheDocument();
  });

  it("shows a grid error, not an endless spinner, when the matrix fails", async () => {
    renderHub("support_bot", { tab: "/policies", matrixError: true });
    // usePolicyMatrix retries 5xx twice (≈3s of backoff) before it errors.
    expect(
      await screen.findByText(/Couldn't load the grid/, undefined, {
        timeout: 5000,
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Loading grid/)).not.toBeInTheDocument();
  });

  it("lists the agent's decisions on the Decision log tab", async () => {
    renderHub("support_bot", { tab: "/decisions" });
    expect(await screen.findByText("Guests can't refund.")).toBeInTheDocument();
    // Scoped, with a way out to the full audit log.
    expect(
      screen.getByRole("link", { name: /full audit log/i }),
    ).toHaveAttribute("href", expect.stringContaining("agent=support_bot"));
  });

  it("links Ban agent to the prefilled /bans route for an admin", async () => {
    renderHub("support_bot");
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
    renderHub("support_bot", { manifestError: true });
    expect(
      await screen.findByText(/Couldn't load this agent/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No agent named/)).not.toBeInTheDocument();
  });
});
