/** The /agents/:name hub: the header above the tabs, the Overview's 24h strip
 * and graph, the Resources tab's manifest and per-role policy, and the
 * unknown-agent case. */

import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Routes } from "react-router-dom";

import { useActive } from "@/lib/active";
import { agentHubRoute } from "@/routes/agent-hub-routes";
import { makeQueryClient, renderWithProviders } from "@/test/render";

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
  return renderWithProviders(<Routes>{agentHubRoute()}</Routes>, {
    initialRoute: `/agents/${name}${tab}`,
  });
}

/** The hub for support_bot, with `/policy/graph` answering `graph`. */
function renderHubWithGraph(graph: unknown, status = 200) {
  stubFetch("admin");
  const answer = vi.mocked(window.fetch).getMockImplementation()!;
  vi.mocked(window.fetch).mockImplementation(async (input, init) =>
    new URL(String(input), "http://localhost").pathname.endsWith(
      "/policy/graph",
    )
      ? new Response(JSON.stringify(graph), {
          status,
          headers: { "Content-Type": "application/json" },
        })
      : answer(input, init),
  );
  return renderWithProviders(<Routes>{agentHubRoute()}</Routes>, {
    initialRoute: "/agents/support_bot",
  });
}

const graphEdge = (
  source: string,
  target: string,
  verdict: "allow" | "deny" = "allow",
) => ({
  source,
  target,
  kind: "call",
  via: null,
  verdict,
  constraints: [],
  roles: [],
});

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
    // 24h activity, each number under its own label.
    const value = async (label: string) =>
      (await screen.findByText(label)).nextElementSibling;
    await waitFor(async () =>
      expect(await value("Calls · 24h")).toHaveTextContent("15"),
    );
    expect(await value("Denied")).toHaveTextContent("4");
    expect(await value("Sent for approval")).toHaveTextContent("1");
  });

  it("marks the counts as old when a refresh fails", async () => {
    stubFetch("admin");
    const client = makeQueryClient();
    client.setQueryData(["audit", "summary", PROJECT, { window: "24h" }], {
      totals: { all: 15, allow: 10, deny: 4, needs_approval: 1 },
      by_agent: [
        { key: "support_bot", all: 15, allow: 10, deny: 4, needs_approval: 1 },
      ],
      by_role: [],
      by_tool: [],
      by_user: [],
    });
    // The next poll fails.
    const answer = vi.mocked(window.fetch).getMockImplementation()!;
    vi.mocked(window.fetch).mockImplementation(async (input, init) =>
      new URL(String(input), "http://localhost").pathname.endsWith(
        "/audit/summary",
      )
        ? new Response("boom", { status: 500 })
        : answer(input, init),
    );
    renderWithProviders(<Routes>{agentHubRoute()}</Routes>, {
      initialRoute: "/agents/support_bot",
      client,
    });
    expect(await screen.findByText(/Couldn't refresh/)).toBeInTheDocument();
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
    // The manifest's tools live on Resources, not the Overview.
    expect(screen.queryByText("refund")).not.toBeInTheDocument();
    const overview = screen.getByRole("link", { name: "Overview" });
    const resources = screen.getByRole("link", { name: /Resources/ });
    expect(overview).toHaveAttribute("aria-current", "page");
    // One tool, no skills.
    expect(resources).toHaveTextContent("Resources1");
    await userEvent.click(resources);
    expect(await screen.findByText("refund")).toBeInTheDocument();
    expect(resources).toHaveAttribute("aria-current", "page");
    expect(overview).not.toHaveAttribute("aria-current");
  });

  it("shows no resource count for an agent that never registered", async () => {
    stubFetch("admin");
    const answer = vi.mocked(window.fetch).getMockImplementation()!;
    vi.mocked(window.fetch).mockImplementation(async (input, init) =>
      new URL(String(input), "http://localhost").pathname.endsWith(
        "/agents/manifest",
      )
        ? new Response(
            JSON.stringify([
              {
                ...MANIFEST,
                manifest: null,
                version: null,
                content_hash: null,
              },
            ]),
            { status: 200, headers: { "Content-Type": "application/json" } },
          )
        : answer(input, init),
    );
    renderWithProviders(<Routes>{agentHubRoute()}</Routes>, {
      initialRoute: "/agents/support_bot",
    });
    expect(await screen.findByText("not registered")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Resources/ })).toHaveTextContent(
      /^Resources$/,
    );
    expect(
      screen.getByText("Resources", { selector: "div" }).nextElementSibling,
    ).toHaveTextContent("—");
  });

  it("shows em-dashes, not fake zeros, when 24h activity fails to load", async () => {
    renderHub("support_bot", { summaryError: true });
    expect(
      await screen.findByText(/Couldn't load the last 24 hours/),
    ).toBeInTheDocument();
    // Calls / Denied / Approvals all unknown (not fake zeros).
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("shows an agent the policy doesn't name through the generic * agent", async () => {
    renderHubWithGraph({
      roles: [],
      nodes: [
        { id: "agent:*", kind: "agent", label: "*" },
        { id: "tool:refund", kind: "tool", label: "refund" },
      ],
      edges: [graphEdge("agent:*", "tool:refund")],
    });
    expect(await screen.findByTestId("agent-graph")).toBeInTheDocument();
    expect(screen.getByText(/can reach — 1 tool$/)).toBeInTheDocument();
  });

  it("counts only what the agent may use, MCP tools apart", async () => {
    renderHubWithGraph({
      roles: [],
      nodes: [
        { id: "agent:support_bot", kind: "agent", label: "support_bot" },
        { id: "tool:refund", kind: "tool", label: "refund" },
        { id: "tool:wipe", kind: "tool", label: "wipe" },
        { id: "tool:mcp-demo-a", kind: "mcp", label: "demo-a" },
        { id: "tool:mcp-demo-b", kind: "mcp", label: "demo-b" },
      ],
      edges: [
        graphEdge("agent:support_bot", "tool:refund"),
        graphEdge("agent:support_bot", "tool:wipe", "deny"),
        graphEdge("agent:support_bot", "tool:mcp-demo-a"),
        graphEdge("agent:support_bot", "tool:mcp-demo-b"),
      ],
    });
    expect(
      await screen.findByText(/can reach — 1 tool, 2 MCP tools$/),
    ).toBeInTheDocument();
  });

  it("marks a cached graph as old when a refetch fails", async () => {
    stubFetch("admin");
    const client = makeQueryClient();
    client.setQueryData(
      ["policy-graph", PROJECT, null],
      {
        roles: [],
        nodes: [
          { id: "agent:support_bot", kind: "agent", label: "support_bot" },
          { id: "tool:refund", kind: "tool", label: "refund" },
        ],
        edges: [graphEdge("agent:support_bot", "tool:refund")],
        // Loaded long ago, so the page refetches it.
      },
      { updatedAt: 0 },
    );
    const answer = vi.mocked(window.fetch).getMockImplementation()!;
    vi.mocked(window.fetch).mockImplementation(async (input, init) =>
      new URL(String(input), "http://localhost").pathname.endsWith(
        "/policy/graph",
      )
        ? new Response(JSON.stringify({ detail: "x" }), { status: 422 })
        : answer(input, init),
    );
    renderWithProviders(<Routes>{agentHubRoute()}</Routes>, {
      initialRoute: "/agents/support_bot",
      client,
    });
    expect(await screen.findByText(/the last one loaded/)).toBeInTheDocument();
    expect(screen.getByTestId("agent-graph")).toBeInTheDocument();
  });

  it("says the policy doesn't compose on a 422", async () => {
    renderHubWithGraph({ detail: "boom" }, 422);
    expect(await screen.findByText(/don't compose/)).toBeInTheDocument();
  });

  it("says the agent has nothing mapped when it isn't in the graph", async () => {
    renderHub("support_bot", { noAgentInGraph: true });
    expect(
      await screen.findByText(
        /Nothing in the policy maps roles, tools or reach/,
      ),
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
