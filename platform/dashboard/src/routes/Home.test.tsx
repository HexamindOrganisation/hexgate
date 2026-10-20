import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useActive } from "@/lib/active";
import { HomePage } from "@/routes/Home";
import { renderWithProviders } from "@/test/render";

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

describe("HomePage", () => {
  beforeEach(() => {
    act(() => useActive.setState({ activeOrgId: "o1", activeProjectId: "p1" }));
  });
  afterEach(() => vi.restoreAllMocks());

  it("leads with Open audit and today's numbers", async () => {
    stub();
    renderWithProviders(<HomePage />);
    expect(
      await screen.findByRole("link", { name: /Open audit/ }),
    ).toHaveAttribute("href", "/audit");
    await waitFor(() =>
      expect(screen.getByText("7.3% deny rate")).toBeInTheDocument(),
    );
    expect(screen.getByText("743K")).toBeInTheDocument();
    expect(screen.getByText(/refund_order/)).toBeInTheDocument();
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
        { id: "t1", name: "ci-staging", last_used_at: "2026-01-01T00:00:00Z" },
      ],
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText("u_8f2c41")).toBeInTheDocument();
    expect(
      screen.getByText("1 call sent for approval today"),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 API key unused/)).toBeInTheDocument();
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
});
