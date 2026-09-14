/**
 * GraphPage tests — the full-page policy graph renders the resource-policy
 * graph (title, role filter, an "Edit policies" jump) for a ready project, and
 * the no-project empty state otherwise. `@xyflow/react` needs a real layout
 * engine jsdom lacks, so it's stubbed to a passthrough.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";

import { GraphPage } from "./Graph";
import { useActive } from "@/lib/active";
import { renderWithProviders } from "@/test/render";

vi.mock("@xyflow/react", () => {
  type P = Record<string, unknown>;
  return {
    ReactFlow: ({ children }: P) => (
      <div data-testid="reactflow">{children as React.ReactNode}</div>
    ),
    Background: () => null,
    Controls: () => null,
    BaseEdge: () => null,
    EdgeLabelRenderer: ({ children }: P) => (
      <div>{children as React.ReactNode}</div>
    ),
    Handle: () => null,
    Panel: ({ children }: P) => <div>{children as React.ReactNode}</div>,
    Position: { Top: "top", Bottom: "bottom", Left: "left", Right: "right" },
    MarkerType: { ArrowClosed: "arrowclosed" },
  };
});

const ORG = {
  id: "org-a",
  slug: "org-a",
  name: "Org A",
  created_at: "2026-01-01T00:00:00Z",
  role: "owner" as const,
};
const PID = "proj-1";

function stubFetch(routes: Record<string, unknown>): void {
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL) => {
      const path = (typeof input === "string" ? input : input.toString()).split(
        "?",
      )[0];
      if (path in routes) {
        return new Response(JSON.stringify(routes[path]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("not found", { status: 404 });
    },
  );
}

afterEach(() => vi.restoreAllMocks());

describe("GraphPage", () => {
  it("renders the policy graph with a role filter and an Edit-policies jump", async () => {
    act(() => {
      useActive.setState({ activeOrgId: ORG.id, activeProjectId: PID });
    });
    stubFetch({
      "/v1/orgs": [ORG],
      "/v1/orgs/org-a/projects": [{ id: PID, name: "proj-1", org_id: ORG.id }],
      [`/v1/projects/${PID}/policy/resolve`]: {
        roles: { support: {}, billing: {} },
      },
      [`/v1/projects/${PID}/policy/graph`]: {
        nodes: [{ id: "agent:bot", kind: "agent", label: "bot" }],
        edges: [],
      },
    });
    renderWithProviders(<GraphPage />);

    expect(await screen.findByText("Policy graph")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /edit policies/i }),
    ).toBeInTheDocument();
    // roles from the resolved policy populate the filter
    await waitFor(() =>
      expect(
        screen.getByRole("option", { name: "support" }),
      ).toBeInTheDocument(),
    );
  });

  it("shows the no-project empty state when none is active", async () => {
    act(() => {
      useActive.setState({ activeOrgId: ORG.id, activeProjectId: null });
    });
    stubFetch({ "/v1/orgs": [ORG] });
    renderWithProviders(<GraphPage />);
    await waitFor(() =>
      expect(screen.getByText(/no project/i)).toBeInTheDocument(),
    );
  });
});
