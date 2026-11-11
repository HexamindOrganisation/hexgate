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
vi.mock(
  "@/components/policy_files/PolicyGraphDialog",
  async (importActual) => ({
    ...(await importActual<
      typeof import("@/components/policy_files/PolicyGraphDialog")
    >()),
    PolicyFlow: ({ graph }: { graph: { nodes: unknown[] } }) => (
      <div data-testid="agent-graph">{graph.nodes.length} nodes</div>
    ),
  }),
);

// The Playground tab embeds the live chat (a WebSocket). Stub it — its own
// behaviour is covered in Playground.test; here we only check the tab mounts
// embedded and project-scoped.
vi.mock("@/routes/Playground", () => ({
  PlaygroundLive: ({
    projectId,
    embedded,
  }: {
    projectId: string;
    embedded?: boolean;
  }) => (
    <div data-testid="embedded-playground">
      playground:{projectId}:{String(embedded)}
    </div>
  ),
}));

// Which agent the serve relay currently exposes. The hub gates the Playground
// tab on this; drive it per test instead of opening a real WebSocket.
const served = vi.hoisted(() => ({
  agentName: null as string | null,
  online: false,
}));
vi.mock("@/lib/playground", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/playground")>()),
  usePlaygroundServedAgent: () => served,
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

/** Per-test answers that win over stubFetch's, keyed by pathname. */
const overrides = new Map<string, { body: unknown; status: number }>();
const answer = (path: string, body: unknown, status = 200) =>
  overrides.set(path, { body, status });

const DECISION = {
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
};

function stubFetch(
  role: string,
  manifestError = false,
  summaryError = false,
  noAgentInGraph = false,
  matrixError = false,
  graph: "default" | "subAgent" | "none" = "default",
  subAgent: false | "tool" | "handoff" = graph === "subAgent" && "tool",
) {
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      const o = overrides.get(url.pathname);
      if (o)
        return new Response(JSON.stringify(o.body), {
          status: o.status,
          headers: { "Content-Type": "application/json" },
        });
      switch (url.pathname) {
        case "/v1/orgs":
          return json([{ id: "org-1", slug: "acme", name: "Acme Inc", role }]);
        case "/v1/orgs/org-1/projects":
          return json([{ id: PROJECT, org_id: "org-1", name: "demo-project" }]);
        case `/v1/projects/${PROJECT}/agents/manifest`:
          if (manifestError) return new Response("boom", { status: 500 });
          return json([
            subAgent
              ? {
                  ...MANIFEST,
                  manifest: {
                    ...MANIFEST.manifest,
                    subagents: [{ name: "billing_bot", via: subAgent }],
                  },
                }
              : MANIFEST,
            // billing_bot is registered when the graph knows it.
            ...(graph === "subAgent"
              ? [{ ...MANIFEST, name: "billing_bot", manifest: null }]
              : []),
          ]);
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
          // A 4xx, which usePolicyMatrix doesn't retry: no backoff in tests.
          if (matrixError) return new Response("boom", { status: 400 });
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
          // A classic (non-compose) project has no graph.
          if (graph === "none") return new Response("nope", { status: 404 });
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
              ...(graph === "subAgent"
                ? [
                    {
                      id: "agent:billing_bot",
                      kind: "agent",
                      label: "billing_bot",
                    },
                  ]
                : []),
            ],
            edges: [
              ...(graph === "subAgent"
                ? [
                    {
                      source: "agent:support_bot",
                      target: "agent:billing_bot",
                      kind: "reach",
                      via: "tool",
                      verdict: "allow",
                      constraints: ["args.amount <= 200"],
                      roles: ["support", "billing"],
                    },
                  ]
                : []),
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
            rows: [DECISION],
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
              {
                key: "billing_bot",
                all: 7,
                allow: 7,
                deny: 0,
                needs_approval: 0,
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
    graph = "default",
    subAgent = graph === "subAgent" && "tool",
    tab = "",
  }: {
    role?: string;
    manifestError?: boolean;
    summaryError?: boolean;
    noAgentInGraph?: boolean;
    matrixError?: boolean;
    graph?: "default" | "subAgent" | "none";
    /** The manifest records billing_bot, reached this way. */
    subAgent?: false | "tool" | "handoff";
    tab?: "" | "/resources" | "/policies" | "/playground" | "/decisions";
  } = {},
) {
  stubFetch(
    role,
    manifestError,
    summaryError,
    noAgentInGraph,
    matrixError,
    graph,
    subAgent,
  );
  return renderWithProviders(<Routes>{agentHubRoute()}</Routes>, {
    initialRoute: `/agents/${name}${tab}`,
  });
}

/** The hub for support_bot, with `/policy/graph` answering `graph`. */
function renderHubWithGraph(graph: unknown, status = 200) {
  stubFetch("admin");
  answer(`/v1/projects/${PROJECT}/policy/graph`, graph, status);
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
    served.agentName = null;
    served.online = false;
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    overrides.clear();
  });

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
    answer(`/v1/projects/${PROJECT}/audit/summary`, "boom", 500);
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
    answer(`/v1/projects/${PROJECT}/agents/manifest`, [
      {
        ...MANIFEST,
        manifest: null,
        version: null,
        content_hash: null,
      },
    ]);
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
    answer(`/v1/projects/${PROJECT}/policy/graph`, { detail: "x" }, 422);
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

  it("lists the manifest's sub-agents with their reach verdict", async () => {
    renderHub("support_bot", { tab: "/resources", graph: "subAgent" });
    const sub = await screen.findByRole("link", { name: "billing_bot" });
    expect(sub).toHaveAttribute("href", "/agents/billing_bot");
    expect(screen.getByText("agent-as-tool")).toBeInTheDocument();
    expect(await screen.findByText(/7 calls · 24h/)).toBeInTheDocument();
    // The verdict overlay comes from the graph's reach edge.
    expect(await screen.findByText("support, billing")).toBeInTheDocument();
    expect(screen.getByTitle("args.amount <= 200")).toHaveTextContent("allow");
    // Counted once: billing_bot isn't already one of its tools (1 + 1).
    expect(screen.getByRole("link", { name: /Resources/ })).toHaveTextContent(
      "Resources2",
    );
  });

  it("lists sub-agents without verdicts on a project with no graph", async () => {
    renderHub("support_bot", {
      tab: "/resources",
      graph: "none",
      subAgent: "tool",
    });
    expect(await screen.findByText("billing_bot")).toBeInTheDocument();
    expect(screen.queryByText("Open graph →")).not.toBeInTheDocument();
    expect(screen.queryByTitle("args.amount <= 200")).not.toBeInTheDocument();
  });

  it("marks a sub-agent as not gated when its policy declares no reach", async () => {
    // The manifest records billing_bot, but the graph has no reach edges.
    renderHub("support_bot", { tab: "/resources", subAgent: "tool" });
    expect(await screen.findByText("not gated")).toBeInTheDocument();
    // Unregistered in the project, so no dead-end link to its hub.
    expect(screen.queryByRole("link", { name: "billing_bot" })).toBeNull();
  });

  it("marks an unnamed sub-agent denied once the policy declares reach", async () => {
    // The graph grants billing_bot as a tool; the manifest reaches it by
    // handoff, which no rule names — closed-world, so denied.
    renderHub("support_bot", {
      tab: "/resources",
      graph: "subAgent",
      subAgent: "handoff",
    });
    expect(await screen.findByTitle(/closed-world/)).toHaveTextContent("deny");
  });

  it("says so when sub-agent call counts can't load", async () => {
    renderHub("support_bot", {
      tab: "/resources",
      graph: "subAgent",
      summaryError: true,
    });
    expect(await screen.findByText("calls unavailable")).toBeInTheDocument();
  });

  it("says when the manifest records no sub-agents", async () => {
    renderHub("support_bot", { tab: "/resources" });
    expect(
      await screen.findByText("No sub-agents recorded on its manifest."),
    ).toBeInTheDocument();
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
    // The project's files are listed.
    expect(screen.getByText("policy.yaml")).toBeInTheDocument();
  });

  it("gives the agent's own tools a row even when the policy doesn't list them", async () => {
    answer(`/v1/projects/${PROJECT}/agents/manifest`, [
      {
        ...MANIFEST,
        manifest: {
          ...MANIFEST.manifest,
          tools: [
            ...MANIFEST.manifest.tools,
            { ...MANIFEST.manifest.tools[0], name: "invoice" },
          ],
        },
      },
    ]);
    answer(`/v1/projects/${PROJECT}/policy/matrix`, {
      roles: ["guest", "support"],
      tools: ["refund"],
      cells: {
        refund: {
          guest: { mode: "deny", constraint: null },
          support: { mode: "allow", constraint: "args.amount <= 50" },
        },
        invoice: {
          guest: { mode: "deny", constraint: null },
          support: { mode: "deny", constraint: null },
        },
      },
      defaults: {
        guest: { mode: "deny", constraint: null },
        support: { mode: "deny", constraint: null },
      },
      aliased_default: null,
    });
    renderHub("support_bot", { tab: "/policies" });
    expect(await screen.findByText("invoice")).toBeInTheDocument();
    expect(screen.getByText("refund")).toBeInTheDocument();
    // Marked as the engine's fallback, not a rule of its own.
    expect(screen.getAllByText("not in the policy")).toHaveLength(1);
  });

  it("says the policy files failed, not that the project is classic", async () => {
    answer(`/v1/projects/${PROJECT}/policy-files`, "boom", 500);
    renderHub("support_bot", { tab: "/policies" });
    expect(
      await screen.findByText(/Couldn't load this project's policy files/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Classic project/)).not.toBeInTheDocument();
  });

  it("shows a grid error, not an endless spinner, when the matrix fails", async () => {
    renderHub("support_bot", { tab: "/policies", matrixError: true });
    expect(
      await screen.findByText(/Couldn't load the grid/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Loading grid/)).not.toBeInTheDocument();
  });

  it("says the decisions failed to load, not that there are none", async () => {
    answer(`/v1/projects/${PROJECT}/audit/decisions`, "boom", 500);
    renderHub("support_bot", { tab: "/decisions" });
    expect(
      await screen.findByText(/Couldn't load this agent's decisions/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No decisions/)).not.toBeInTheDocument();
  });

  it("says there were no decisions in the last 24h", async () => {
    answer(`/v1/projects/${PROJECT}/audit/decisions`, { rows: [], total: 0 });
    renderHub("support_bot", { tab: "/decisions" });
    expect(
      await screen.findByText("No decisions in the last 24h."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/match the current filters/),
    ).not.toBeInTheDocument();
  });

  it("keeps the loaded decisions when loading more fails", async () => {
    const path = `/v1/projects/${PROJECT}/audit/decisions`;
    answer(path, { rows: [DECISION], total: 100, limit: 40, offset: 0 });
    renderHub("support_bot", { tab: "/decisions" });
    expect(await screen.findByText("Guests can't refund.")).toBeInTheDocument();
    answer(path, "boom", 500);
    await userEvent.click(screen.getByRole("button", { name: /Load 40 more/ }));
    expect(
      await screen.findByText(/these are the last decisions loaded/),
    ).toBeInTheDocument();
    expect(screen.getByText("Guests can't refund.")).toBeInTheDocument();
    // Trying again asks for the failed page, not the one after it.
    const limits = () =>
      vi
        .mocked(window.fetch)
        .mock.calls.map(([u]) => new URL(String(u), "http://localhost"))
        .filter((u) => u.pathname === path)
        .map((u) => u.searchParams.get("limit"));
    expect(limits().at(-1)).toBe("80");
    await userEvent.click(screen.getByRole("button", { name: /Load 40 more/ }));
    await waitFor(() =>
      expect(limits().filter((l) => l === "80")).toHaveLength(2),
    );
    expect(limits()).not.toContain("120");
  });

  it("lists the agent's decisions on the Decision log tab", async () => {
    renderHub("support_bot", { tab: "/decisions" });
    expect(await screen.findByText("Guests can't refund.")).toBeInTheDocument();
    // Scoped, with a way out to the full audit log.
    expect(
      screen.getByRole("link", { name: /full audit log/i }),
    ).toHaveAttribute("href", expect.stringContaining("agent=support_bot"));
  });

  it("embeds the playground on the tab when this agent is being served", async () => {
    served.agentName = "support_bot";
    served.online = true;
    renderHub("support_bot", { tab: "/playground" });
    // Embedded (not full-screen) and keyed to this project.
    expect(await screen.findByTestId("embedded-playground")).toHaveTextContent(
      `playground:${PROJECT}:true`,
    );
    // Framed as a live session with this agent.
    expect(screen.getByText(/Live session with/)).toBeInTheDocument();
  });

  it("mutes the Playground tab when this agent isn't served", async () => {
    // served is off by default — the nav tab is a disabled span, not a link.
    renderHub("support_bot");
    expect(
      await screen.findByRole("heading", { name: "support_bot" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Playground" }),
    ).not.toBeInTheDocument();
    const muted = screen.getByText("Playground", { selector: "span" });
    expect(muted).toHaveAttribute("aria-disabled", "true");
  });

  it("shows a not-served state, not a live chat, when this agent isn't served", async () => {
    renderHub("support_bot", { tab: "/playground" });
    expect(await screen.findByText(/isn't being served/)).toBeInTheDocument();
    expect(screen.queryByTestId("embedded-playground")).not.toBeInTheDocument();
  });

  it("names the agent currently being served when it isn't this one", async () => {
    served.agentName = "other_bot";
    served.online = true;
    renderHub("support_bot", { tab: "/playground" });
    expect(await screen.findByText(/currently serving/)).toBeInTheDocument();
    expect(screen.getByText("other_bot")).toBeInTheDocument();
    expect(screen.queryByTestId("embedded-playground")).not.toBeInTheDocument();
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
