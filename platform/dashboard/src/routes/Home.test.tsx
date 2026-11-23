import { act, screen, waitFor } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useActive } from "@/lib/active";
import { useUi } from "@/lib/ui";
import { HomePage } from "@/routes/Home";
import { makeQueryClient, renderWithProviders } from "@/test/render";

const ORG = {
  id: "o1",
  slug: "o1",
  name: "Org",
  created_at: "",
  role: "owner",
};
const PROJECT = { id: "p1", org_id: "o1", name: "support-bot", created_at: "" };
const counts = (allow: number, deny: number, appr: number) => ({
  all: allow + deny + appr,
  allow,
  deny,
  needs_approval: appr,
});
const NOW = Date.now();

function stub(overrides: Record<string, unknown> = {}) {
  const routes: Record<string, unknown> = {
    "/v1/orgs": [ORG],
    "/v1/orgs/o1/projects": [PROJECT],
    "/v1/projects/p1/audit/summary": {
      totals: counts(50, 4, 1),
      by_agent: [{ key: "support-bot", ...counts(30, 3, 0) }],
      by_role: [],
      by_tool: [],
      by_user: [],
    },
    "/v1/projects/p1/audit/timeseries": [],
    "/v1/projects/p1/audit/decisions": {
      rows: [
        {
          event_id: "e1",
          occurred_at: new Date(NOW - 8000).toISOString(),
          received_at: "",
          agent_name: "support-bot",
          agent_version_id: "",
          session_id: "",
          user_id: "",
          tool_name: "refund_order",
          user_roles: [],
          deciding_role: "",
          outcome: "deny",
          error_type: "",
          reason: "",
          violations: [],
          hint: null,
          arguments: { amount: 400 },
          attributes: null,
          run_id: null,
        },
      ],
      total: 1,
      limit: 7,
      offset: 0,
    },
    "/v1/projects/p1/audit/anomalies": [],
    "/v1/projects/p1/agents/manifest": [
      {
        name: "support-bot",
        manifest: { tools: [] },
        version: 1,
        content_hash: "",
        updated_at: "",
      },
    ],
    "/v1/projects/p1/tokens": [],
    "/v1/projects/p1/llm/summary": {
      totals: {
        calls: 188,
        input_tokens: 0,
        output_tokens: 0,
        total_tokens: 743000,
      },
      by_model: [],
      by_agent: [],
      by_user: [],
    },
    ...overrides,
  };
  vi.spyOn(window, "fetch").mockImplementation(async (input) => {
    const path = new URL(String(input), "http://x").pathname;
    const body = routes[path];
    return new Response(JSON.stringify(body ?? null), {
      status: body === undefined ? 404 : 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

/** Leave `path`'s request hanging; everything else answers as `stub` set. */
function stall(path: string) {
  const answer = vi.mocked(window.fetch).getMockImplementation()!;
  vi.mocked(window.fetch).mockImplementation((input, init) =>
    new URL(String(input), "http://x").pathname === path
      ? new Promise(() => {})
      : answer(input, init),
  );
}

/** Wait until Home has the org list and the active org's project list, so an
 * assertion that the quickstart stayed closed isn't made before they land. */
async function listsLoaded(client: QueryClient) {
  await waitFor(() => {
    expect(client.getQueryData(["orgs"])).toBeDefined();
    expect(client.getQueryData(["projects", "o1"])).toBeDefined();
  });
}

describe("HomePage", () => {
  beforeEach(() => {
    act(() => useActive.setState({ activeOrgId: "o1", activeProjectId: "p1" }));
    useUi.setState({ quickstartOpen: false, quickstartSeen: false });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("leads with Open audit and today's numbers", async () => {
    stub();
    renderWithProviders(<HomePage />);
    expect(
      await screen.findByRole("link", { name: /Open audit/ }),
      // Reopens Audit as the user left it; only deep links reset.
    ).toHaveAttribute("href", "/audit");
    await waitFor(() =>
      expect(screen.getByText("7.3% deny rate")).toBeInTheDocument(),
    );
    expect(screen.getByText("743.0K")).toBeInTheDocument();
    expect(screen.getByText(/refund_order/)).toBeInTheDocument();
  });

  const agentsNamed = (names: string[]) =>
    names.map((name) => ({
      name,
      manifest: { tools: [] },
      version: 1,
      content_hash: "",
      updated_at: "",
    }));
  const agentRows = () =>
    screen
      .getAllByRole("link")
      .filter((l) => l.getAttribute("href")?.startsWith("/agents/"));

  it("caps the agents at the most denied, then busiest", async () => {
    const names = Array.from({ length: 10 }, (_, i) => `bot-${i}`);
    stub({
      "/v1/projects/p1/agents/manifest": agentsNamed(names),
      "/v1/projects/p1/audit/summary": {
        totals: counts(1050, 5, 0),
        by_agent: [
          { key: "bot-8", ...counts(1000, 0, 0) },
          // Fewer calls, but denied ones: it leads.
          { key: "bot-9", ...counts(50, 5, 0) },
        ],
        by_role: [],
        by_tool: [],
        by_user: [],
      },
    });
    renderWithProviders(<HomePage />);
    expect(
      await screen.findByRole("link", { name: /View all 10/ }),
    ).toHaveAttribute("href", "/agents");
    await waitFor(() => expect(agentRows()[0]).toHaveTextContent("bot-9"));
    expect(agentRows()).toHaveLength(8);
    expect(agentRows()[1]).toHaveTextContent("bot-8");
    expect(screen.getByText("top 8 of 10")).toBeInTheDocument();
    // Quiet agents past the cap are left out.
    expect(screen.queryByRole("link", { name: /bot-7/ })).toBeNull();
  });

  it("lists every agent, with no View all, up to the cap", async () => {
    const names = Array.from({ length: 8 }, (_, i) => `bot-${i}`);
    stub({ "/v1/projects/p1/agents/manifest": agentsNamed(names) });
    renderWithProviders(<HomePage />);
    expect(await screen.findByRole("link", { name: "Manage" })).toBeVisible();
    await waitFor(() => expect(agentRows()).toHaveLength(8));
    expect(screen.queryByRole("link", { name: /View all \d/ })).toBeNull();
  });

  it("lists what needs attention: bursts, approvals and stale keys", async () => {
    stub({
      "/v1/projects/p1/audit/anomalies": [
        {
          user_id: "u_8f2c41",
          severity: "high",
          deny: 19,
          all: 22,
          deny_rate: 0.86,
          first_seen: new Date(NOW - 36e5).toISOString(),
          last_seen: new Date(NOW - 18e5).toISOString(),
        },
      ],
      "/v1/projects/p1/tokens": [
        {
          id: "t1",
          name: "ci-staging",
          created_at: "2025-12-01T00:00:00Z",
          last_used_at: "2026-01-01T00:00:00Z",
        },
        // Minted a moment ago and never used: not stale.
        {
          id: "t2",
          name: "fresh",
          created_at: new Date(NOW - 6e4).toISOString(),
          last_used_at: null,
        },
      ],
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText("u_8f2c41")).toBeInTheDocument();
    expect(
      screen.getByText("1 call sent for approval in the last 24 hours"),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 API key unused/)).toBeInTheDocument();
    expect(screen.getByText("ci-staging")).toBeInTheDocument();
    expect(screen.queryByText("fresh")).not.toBeInTheDocument();
  });

  it("links a burst to Audit filtered to its user and days", async () => {
    stub({
      "/v1/projects/p1/audit/anomalies": [
        {
          user_id: "u_8f2c41",
          severity: "high",
          deny: 19,
          all: 22,
          deny_rate: 0.86,
          first_seen: new Date(NOW - 36e5).toISOString(),
          last_seen: new Date(NOW - 18e5).toISOString(),
        },
      ],
    });
    renderWithProviders(<HomePage />);
    const link = (await screen.findByText("u_8f2c41")).closest("a")!;
    const params = new URL(link.getAttribute("href")!, "http://x").searchParams;
    expect(params.get("user")).toBe("u_8f2c41");
    expect(params.get("from")).not.toBeNull();
    expect(params.get("agent")).toBeNull();
  });

  it("does not call it all clear while signals are still loading", async () => {
    // Every other signal lands; only the bursts are still on their way.
    stub();
    stall("/v1/projects/p1/audit/anomalies");
    renderWithProviders(<HomePage />);
    expect(await screen.findByText("Checking…")).toBeInTheDocument();
    await screen.findByText(/last decision/);
    expect(screen.getByText("Checking…")).toBeInTheDocument();
    expect(
      screen.queryByText("Nothing needs a look right now."),
    ).not.toBeInTheDocument();
  });

  it("waits for the API keys before calling it all clear", async () => {
    stub();
    stall("/v1/projects/p1/tokens");
    renderWithProviders(<HomePage />);
    // Summary, bursts and agents have all landed: only the keys are left.
    await screen.findByText(/sent for approval/);
    await screen.findByText(/last decision/);
    expect(screen.getByText("Checking…")).toBeInTheDocument();
  });

  it("says the list may be incomplete when the keys fail to load", async () => {
    stub({ "/v1/projects/p1/tokens": undefined });
    renderWithProviders(<HomePage />);
    expect(
      await screen.findByText(/may be incomplete/, {}, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it("says so when nothing needs a look", async () => {
    stub({
      "/v1/projects/p1/audit/summary": {
        totals: counts(5, 0, 0),
        by_agent: [],
        by_role: [],
        by_tool: [],
        by_user: [],
      },
    });
    renderWithProviders(<HomePage />);
    expect(
      await screen.findByText("Nothing needs a look right now."),
    ).toBeInTheDocument();
  });

  it("shows the setup checklist while the project has no API key", async () => {
    stub();
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/2 of 3 done/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /mint a key/i })).toHaveAttribute(
      "href",
      "/tokens?mint=1",
    );
  });

  it("hides the checklist once a key, an agent and a decision exist", async () => {
    stub({
      "/v1/projects/p1/tokens": [
        {
          id: "k1",
          name: "prod",
          masked: "fty_live_…abcd",
          scopes: [],
          created_at: new Date(NOW).toISOString(),
          last_used_at: new Date(NOW).toISOString(),
          created_by_user_id: null,
          created_by_email: null,
          owner_user_id: null,
          owner_email: null,
        },
      ],
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/refund_order/)).toBeInTheDocument();
    // "Checking…" clears only once the keys (and every other signal) land.
    await waitFor(() => expect(screen.queryByText("Checking…")).toBeNull());
    expect(screen.queryByText(/of 3 done/)).toBeNull();
  });

  it("offers the quickstart once when the project has no agents", async () => {
    stub({ "/v1/projects/p1/agents/manifest": [] });
    renderWithProviders(<HomePage />);
    await waitFor(() => expect(useUi.getState().quickstartOpen).toBe(true));
  });

  it("offers the quickstart to a new account, whose org has no project yet", async () => {
    act(() => useActive.setState({ activeProjectId: null }));
    stub({ "/v1/orgs/o1/projects": [] });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText("No project selected")).toBeInTheDocument();
    await waitFor(() => expect(useUi.getState().quickstartOpen).toBe(true));
  });

  it("leaves an open quickstart on its step when a new project's agents load", async () => {
    // A new user on step 2 creates a project: its agent list loads empty,
    // which is the first-run state again, but the dialog is already open.
    useUi.setState({ quickstartOpen: true, quickstartStep: 1 });
    stub({ "/v1/projects/p1/agents/manifest": [] });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/No agents yet/)).toBeInTheDocument();
    expect(useUi.getState().quickstartStep).toBe(1);
  });

  it("doesn't offer it while an existing user's default project is picked", async () => {
    // Projects exist, but none is active yet (a fresh browser, before the
    // shell's bootstrap picks one): the page shows "no project" for a moment.
    act(() => useActive.setState({ activeProjectId: null }));
    stub();
    const client = makeQueryClient();
    renderWithProviders(<HomePage />, { client });
    expect(await screen.findByText("No project selected")).toBeInTheDocument();
    // Once both lists are loaded (one org, one project), still no auto-open.
    await listsLoaded(client);
    expect(useUi.getState().quickstartOpen).toBe(false);
  });

  it("doesn't offer it over an invited user's empty personal org", async () => {
    // Sign-up gives everyone a personal org; an invited user's real projects
    // are in the team org, so an empty first org isn't a first run.
    act(() => useActive.setState({ activeProjectId: null }));
    stub({
      "/v1/orgs": [ORG, { ...ORG, id: "o2", slug: "team", name: "Team" }],
      "/v1/orgs/o1/projects": [],
    });
    const client = makeQueryClient();
    renderWithProviders(<HomePage />, { client });
    expect(await screen.findByText("No project selected")).toBeInTheDocument();
    await listsLoaded(client);
    expect(useUi.getState().quickstartOpen).toBe(false);
  });

  it("doesn't reopen the quickstart once it has been seen", async () => {
    useUi.setState({ quickstartSeen: true });
    stub({ "/v1/projects/p1/agents/manifest": [] });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/No agents yet/)).toBeInTheDocument();
    expect(useUi.getState().quickstartOpen).toBe(false);
  });

  const asked90d = () =>
    vi
      .mocked(window.fetch)
      .mock.calls.map(([u]) => String(u))
      .some((u) => u.includes("/audit/summary") && u.includes("window=90d"));

  it("ticks the decision step from recent activity without a 90-day scan", async () => {
    stub();
    renderWithProviders(<HomePage />);
    // Key to do, agent and decision done.
    expect(await screen.findByText(/2 of 3 done/)).toBeInTheDocument();
    expect(asked90d()).toBe(false);
  });

  it("falls back to the 90-day audit totals when the last week is quiet", async () => {
    stub();
    const answer = vi.mocked(window.fetch).getMockImplementation()!;
    vi.mocked(window.fetch).mockImplementation((input, init) => {
      const url = String(input);
      if (!url.includes("/audit/summary")) return answer(input, init);
      const all = url.includes("window=90d") ? 3 : 0;
      return Promise.resolve(
        new Response(
          JSON.stringify({
            totals: counts(all, 0, 0),
            by_agent: [],
            by_role: [],
            by_tool: [],
            by_user: [],
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/2 of 3 done/)).toBeInTheDocument();
    expect(asked90d()).toBe(true);
  });

  it("doesn't pop the quickstart over agents that exist but aren't registered", async () => {
    stub({
      "/v1/projects/p1/agents/manifest": [
        {
          name: "draft",
          manifest: null,
          version: null,
          content_hash: "",
          updated_at: "",
        },
      ],
    });
    renderWithProviders(<HomePage />);
    // The checklist still asks for a registered agent…
    expect(await screen.findByText(/1 of 3 done/)).toBeInTheDocument();
    // …but only a truly empty project gets the walkthrough unprompted.
    expect(useUi.getState().quickstartOpen).toBe(false);
  });

  it("shows no tick for the decision step when audit is unavailable", async () => {
    // No route for the summary: the 24h panel and the 90-day check both 404.
    stub({ "/v1/projects/p1/audit/summary": undefined });
    renderWithProviders(<HomePage />);
    // Key (to do) and agent (done) still count; the decision step doesn't.
    expect(
      await screen.findByText(/1 of 2 done/, {}, { timeout: 5000 }),
    ).toBeInTheDocument();
  });

  it("points a project that never had a decision at the quickstart", async () => {
    stub({
      "/v1/projects/p1/audit/summary": {
        totals: counts(0, 0, 0),
        by_agent: [],
        by_role: [],
        by_tool: [],
        by_user: [],
      },
      "/v1/projects/p1/audit/decisions": {
        rows: [],
        total: 0,
        limit: 7,
        offset: 0,
      },
    });
    renderWithProviders(<HomePage />);
    expect(
      await screen.findByRole("button", { name: "Getting started" }),
    ).toBeInTheDocument();
  });
});
