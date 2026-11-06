/**
 * Tests for the /agents/:name hub — the agent-first page. It composes the
 * manifest (via ManifestView), the per-role policy (engine matrix) and the
 * 24h activity for one agent, and handles the unknown-agent case.
 */

import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes, useLocation } from "react-router-dom";

import { useActive } from "@/lib/active";
import {
  AgentDecisionsTab,
  AgentHub,
  AgentOverviewTab,
  AgentPlaygroundTab,
  AgentPoliciesTab,
  AgentResourcesTab,
} from "@/routes/AgentHub";
import { AgentStatsTab } from "@/routes/AgentStats";
import { renderWithProviders } from "@/test/render";

// Stub the heavy graph (ReactFlow) — the hub only needs it to mount with the
// agent's subgraph; PolicyFlow and the dialog's agent narrowing are tested in
// PolicyGraphDialog.test, so the dialog stub only records what it opens with.
vi.mock(
  "@/components/policy_files/PolicyGraphDialog",
  async (importActual) => ({
    ...(await importActual<
      typeof import("@/components/policy_files/PolicyGraphDialog")
    >()),
    PolicyFlow: ({ graph }: { graph: { nodes: unknown[] } }) => (
      <div data-testid="agent-graph">{graph.nodes.length} nodes</div>
    ),
    PolicyGraphDialog: ({ open, agent }: { open: boolean; agent?: string }) =>
      open ? <div role="dialog" aria-label={`${agent} policy graph`} /> : null,
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

// Every URL the stub served, for asserting the query a tab sent.
const fetched: URL[] = [];

function stubFetch(
  role: string,
  manifestError = false,
  summaryError = false,
  noAgentInGraph = false,
  matrixError = false,
  graph: "default" | "subAgent" | "none" = "default",
  subAgent: false | "tool" | "handoff" = graph === "subAgent" && "tool",
  tsError = false,
) {
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      fetched.push(url);
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
        case `/v1/projects/${PROJECT}/audit/timeseries`:
          if (tsError) return new Response("boom", { status: 500 });
          return json([
            {
              bucket: "2026-11-05T10:00:00Z",
              allow: 4,
              deny: 1,
              needs_approval: 0,
            },
            {
              bucket: "2026-11-05T11:00:00Z",
              allow: 6,
              deny: 3,
              needs_approval: 1,
            },
          ]);
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
    tsError = false,
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
    tsError?: boolean;
    tab?:
      | ""
      | "/stats"
      | "/stats?range=7d"
      | "/resources"
      | "/policies"
      | "/playground"
      | "/decisions";
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
    tsError,
  );
  return renderWithProviders(
    <Routes>
      <Route path="/agents/:name" element={<AgentHub />}>
        <Route index element={<AgentOverviewTab />} />
        <Route path="stats" element={<AgentStatsTab />} />
        <Route path="resources" element={<AgentResourcesTab />} />
        <Route path="policies" element={<AgentPoliciesTab />} />
        <Route path="playground" element={<AgentPlaygroundTab />} />
        <Route path="decisions" element={<AgentDecisionsTab />} />
      </Route>
      <Route path="/audit" element={<AuditProbe />} />
    </Routes>,
    { initialRoute: `/agents/${name}${tab}` },
  );
}

/** Stands in for /audit: shows the filters a drill-down navigated with. */
function AuditProbe() {
  return <div data-testid="audit-url">{useLocation().search}</div>;
}

describe("AgentHub", () => {
  beforeEach(() => {
    fetched.length = 0;
    served.agentName = null;
    served.online = false;
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

  it("expands the Overview graph into a dialog scoped to the agent", async () => {
    renderHub("support_bot");
    expect(await screen.findByTestId("agent-graph")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Expand/ }));
    expect(
      await screen.findByRole("dialog", { name: "support_bot policy graph" }),
    ).toBeInTheDocument();
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

  it("shows the agent's audit stats, scoped to it, on the Stats tab", async () => {
    renderHub("support_bot", { tab: "/stats" });
    expect(await screen.findByText("Decisions per hour")).toBeInTheDocument();
    expect(screen.getByText("26.7% deny rate")).toBeInTheDocument();
    // Both the summary and the timeseries carry the agent filter (the hub's
    // own 24h summary is unscoped, so look for the scoped request).
    for (const path of ["audit/summary", "audit/timeseries"]) {
      expect(
        fetched.some(
          (u) =>
            u.pathname.endsWith(path) &&
            u.searchParams.get("agent") === "support_bot",
        ),
      ).toBe(true);
    }
  });

  it("refetches the Stats tab for a new range", async () => {
    renderHub("support_bot", { tab: "/stats" });
    await screen.findByText("Decisions per hour");
    await userEvent.click(screen.getByText("7d"));
    await vi.waitFor(() =>
      expect(
        fetched.some(
          (u) =>
            u.pathname.endsWith("audit/timeseries") &&
            u.searchParams.get("window") === "7d" &&
            u.searchParams.get("agent") === "support_bot",
        ),
      ).toBe(true),
    );
  });

  it("drills a Stats tile into /audit filtered to the agent", async () => {
    renderHub("support_bot", { tab: "/stats" });
    await userEvent.click(
      await screen.findByRole("button", { name: /Denied/ }),
    );
    const search = new URLSearchParams(
      (await screen.findByTestId("audit-url")).textContent ?? "",
    );
    expect(search.get("agent")).toBe("support_bot");
    expect(search.get("outcome")).toBe("deny");
    expect(search.get("range")).toBe("24h");
  });

  it("reads the Stats range from the URL", async () => {
    renderHub("support_bot", { tab: "/stats?range=7d" });
    await screen.findByText("26.7% deny rate");
    expect(
      fetched.some(
        (u) =>
          u.pathname.endsWith("audit/summary") &&
          u.searchParams.get("agent") === "support_bot" &&
          u.searchParams.get("window") === "7d",
      ),
    ).toBe(true);
    expect(screen.getByRole("radio", { name: "7d" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("says the chart failed, not that there were no decisions", async () => {
    renderHub("support_bot", { tab: "/stats", tsError: true });
    expect(
      await screen.findByText(/Couldn't load the chart/),
    ).toBeInTheDocument();
    expect(screen.queryByText("No decisions in this range.")).toBeNull();
  });

  it("says so when the Stats tab can't load", async () => {
    renderHub("support_bot", { tab: "/stats", summaryError: true });
    expect(
      await screen.findByText(/Couldn't load this agent's stats/),
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

  it("opens the agent's graph dialog from Reaches", async () => {
    renderHub("support_bot", { tab: "/resources", graph: "subAgent" });
    await userEvent.click(
      await screen.findByRole("button", { name: "Open graph →" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "support_bot policy graph" }),
    ).toBeInTheDocument();
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
