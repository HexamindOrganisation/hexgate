/** The /agents list: each row links into the hub, with 24h volume and
 * delegation, the KPI strip, and the legacy ?agent= redirect. */

import { act, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes, useParams } from "react-router-dom";

import { useActive } from "@/lib/active";
import { AgentsPage } from "@/routes/Agents";
import { makeQueryClient, renderWithProviders } from "@/test/render";

const PROJECT = "p1";

const MANIFEST = {
  name: "support_bot",
  manifest: {
    name: "support_bot",
    description: null,
    framework: "langchain",
    model: null,
    system_prompt: null,
    tools: [],
    skills: null,
    guards: null,
  },
  version: 1,
  content_hash: "hash-1",
  updated_at: "2026-06-01T10:00:00Z",
};

function stubFetch(manifest: unknown = MANIFEST) {
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
          return json([
            {
              id: "org-1",
              slug: "acme",
              name: "Acme Inc",
              created_at: "2026-01-01T00:00:00Z",
              role: "admin",
            },
          ]);
        case "/v1/orgs/org-1/projects":
          return json([
            {
              id: PROJECT,
              org_id: "org-1",
              name: "demo-project",
              created_at: "2026-01-01T00:00:00Z",
            },
          ]);
        case `/v1/projects/${PROJECT}/agents/manifest`:
          return manifest === "error"
            ? new Response("boom", { status: 500 })
            : json(manifest === "empty" ? [] : [manifest]);
        case `/v1/projects/${PROJECT}/policy/graph`:
          // support_bot reaches downstream_bot and is reached by upstream_bot.
          return json({
            roles: [],
            nodes: [
              { id: "agent:support_bot", kind: "agent", label: "support_bot" },
              {
                id: "agent:downstream_bot",
                kind: "agent",
                label: "downstream_bot",
              },
              {
                id: "agent:upstream_bot",
                kind: "agent",
                label: "upstream_bot",
              },
            ],
            edges: [
              {
                source: "agent:support_bot",
                target: "agent:downstream_bot",
                kind: "reach",
                via: "tool",
                verdict: "allow",
                constraints: [],
                roles: [],
              },
              {
                source: "agent:upstream_bot",
                target: "agent:support_bot",
                kind: "reach",
                via: "tool",
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

/** Stand-in for the agent hub, so the redirect test can prove where it lands. */
function HubStub() {
  const { name } = useParams();
  return <div>hub:{name}</div>;
}

describe("AgentsPage — the agent list", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("lists each agent as a link into its hub", async () => {
    stubFetch();
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    const link = await screen.findByRole("link", { name: "support_bot" });
    expect(link).toHaveAttribute("href", "/agents/support_bot");
  });

  it("leads with a portfolio KPI strip from the 24h summary", async () => {
    stubFetch();
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    // Await a summary-derived value (the labels render before it loads):
    // 4 of 15 denied → the rate rides on the Denied tile.
    expect(await screen.findByText(/26\.7%/)).toBeInTheDocument();
    // Strip labels are distinct from the row's lowercase "calls 24h"/"denied".
    expect(screen.getByText("Calls · 24h")).toBeInTheDocument();
    expect(screen.getByText("across all agents")).toBeInTheDocument();
    expect(screen.getByText("Denied · 24h")).toBeInTheDocument();
    expect(screen.getByText("Needs approval · 24h")).toBeInTheDocument();
  });

  it("shows each agent's 24h call and denial volume on its row", async () => {
    stubFetch();
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    const row = await screen.findByRole("link", { name: "support_bot" });
    expect(within(row).getByText("15")).toBeInTheDocument();
    expect(within(row).getByText("calls 24h")).toBeInTheDocument();
    expect(within(row).getByText("4")).toBeInTheDocument();
    expect(within(row).getByText("denied")).toBeInTheDocument();
  });

  it("shows a running but unregistered agent's 24h activity", async () => {
    stubFetch({
      ...MANIFEST,
      manifest: null,
      version: null,
      content_hash: null,
    });
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    const row = await screen.findByRole("link", { name: "support_bot" });
    expect(within(row).getByText(/run/)).toHaveTextContent(
      "not registered · run hexgate register",
    );
    expect(await within(row).findByText("15")).toBeInTheDocument();
    expect(
      screen.getByText("0 registered · 1 not registered"),
    ).toBeInTheDocument();
  });

  it("says the 24h activity failed to load, not just dashes", async () => {
    stubFetch();
    const answer = vi.mocked(window.fetch).getMockImplementation()!;
    vi.mocked(window.fetch).mockImplementation(async (input, init) =>
      new URL(String(input), "http://localhost").pathname.endsWith(
        "/audit/summary",
      )
        ? new Response("boom", { status: 500 })
        : answer(input, init),
    );
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    expect(
      await screen.findByText("Couldn't load the last 24 hours of activity."),
    ).toBeInTheDocument();
  });

  it("marks the counts as old when a later refresh fails", async () => {
    const client = makeQueryClient();
    client.setQueryData(
      ["audit", "summary", PROJECT, { window: "24h" }],
      {
        totals: { all: 7, allow: 7, deny: 0, needs_approval: 0 },
        by_agent: [],
        by_role: [],
        by_tool: [],
        by_user: [],
      },
      // Loaded long ago, so the page refetches it.
      { updatedAt: 0 },
    );
    stubFetch();
    const answer = vi.mocked(window.fetch).getMockImplementation()!;
    vi.mocked(window.fetch).mockImplementation(async (input, init) =>
      new URL(String(input), "http://localhost").pathname.endsWith(
        "/audit/summary",
      )
        ? new Response("boom", { status: 500 })
        : answer(input, init),
    );
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents", client });
    expect(
      await screen.findByText(/this is what was last loaded/),
    ).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("says why delegation is missing when the policy doesn't compose", async () => {
    stubFetch();
    const answer = vi.mocked(window.fetch).getMockImplementation()!;
    vi.mocked(window.fetch).mockImplementation(async (input, init) =>
      new URL(String(input), "http://localhost").pathname.endsWith(
        "/policy/graph",
      )
        ? new Response(JSON.stringify({ detail: "x" }), { status: 422 })
        : answer(input, init),
    );
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    expect(
      await screen.findByText(/the policy modules don't compose/),
    ).toBeInTheDocument();
  });

  it("shows an unregistered agent's own reach", async () => {
    stubFetch({
      ...MANIFEST,
      manifest: null,
      version: null,
      content_hash: null,
    });
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    const row = await screen.findByRole("link", { name: "support_bot" });
    expect(
      await within(row).findByText(/reaches downstream_bot/),
    ).toBeInTheDocument();
  });

  it("surfaces agent-to-agent delegation from the policy graph", async () => {
    stubFetch();
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    const row = await screen.findByRole("link", { name: "support_bot" });
    // Outbound reach rides in the meta line; inbound is a "called by" chip.
    expect(
      await within(row).findByText(/reaches downstream_bot/),
    ).toBeInTheDocument();
    expect(within(row).getByText(/called by upstream_bot/)).toBeInTheDocument();
  });

  it("sends a legacy ?agent= deep link to the agent's hub", async () => {
    stubFetch();
    renderWithProviders(
      <Routes>
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/agents/:name" element={<HubStub />} />
      </Routes>,
      { initialRoute: "/agents?agent=support_bot" },
    );
    expect(await screen.findByText("hub:support_bot")).toBeInTheDocument();
  });

  it("does not claim there are no agents while the project resolves", async () => {
    vi.spyOn(window, "fetch").mockImplementation(() => new Promise(() => {}));
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    expect(await screen.findByText("Loading agents…")).toBeInTheDocument();
    expect(screen.queryByText("No agents yet.")).not.toBeInTheDocument();
  });

  it("says when agents couldn't load instead of claiming there are none", async () => {
    stubFetch("error");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    expect(
      await screen.findByText("Couldn't load agents."),
    ).toBeInTheDocument();
    expect(screen.queryByText("No agents yet.")).not.toBeInTheDocument();
  });

  it("says there are no agents when the list is empty", async () => {
    stubFetch("empty");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    expect(await screen.findByText(/No agents yet/)).toBeInTheDocument();
  });
});
