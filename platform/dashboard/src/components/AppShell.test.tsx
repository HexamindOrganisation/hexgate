/**
 * Tests for the sidebar app-shell.
 *
 * Four surfaces worth pinning:
 *   1. The workspace nav renders its links, and collapsing the sidebar
 *      hides the labels (icon-only mode).
 *   2. The collapse toggle works from both the button and Cmd/Ctrl-B.
 *   3. The account chip shows the signed-in user and signs out.
 *   4. The bootstrap effect auto-picks a first org + project, and heals
 *      a stale (removed-org / deleted-project) selection.
 *
 * The dropdown/label plumbing inside OrgProjectSwitcher has its own
 * test — here we only assert the shell's own behaviour.
 */

import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/AppShell";
import { useActive } from "@/lib/active";
import { useUi } from "@/lib/ui";
import { renderWithProviders } from "@/test/render";

const USER = { id: "user-1", email: "dev@example.com", is_verified: true };
const ORG_A = {
  id: "org-a",
  slug: "org-a",
  name: "Org Alpha",
  created_at: "2026-01-01T00:00:00Z",
  role: "owner" as const,
};
const ORG_B = {
  id: "org-b",
  slug: "org-b",
  name: "Org Beta",
  created_at: "2026-01-02T00:00:00Z",
  role: "member" as const,
};
const PROJ_A1 = { id: "proj-a1", name: "alpha-one", org_id: "org-a" };
const PROJ_A2 = { id: "proj-a2", name: "alpha-two", org_id: "org-a" };

/** Stub fetch with canned JSON keyed by path; 404 for anything else so
 * a missed route surfaces as an obvious failure rather than a hang. */
function stubFetch(routes: Record<string, unknown>): void {
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      const path = url.split("?")[0];
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

/** A fully-wired workspace: a signed-in user, two orgs, two projects
 * under org-a. Individual tests seed the active selection they want. */
function stubWorkspace(): void {
  stubFetch({
    "/v1/users/me": USER,
    "/v1/orgs": [ORG_A, ORG_B],
    "/v1/orgs/org-a/projects": [PROJ_A1, PROJ_A2],
    "/v1/orgs/org-b/projects": [],
    "/v1/auth/cookie/logout": {},
    "/v1/auth/google/authorize": {},
  });
}

describe("AppShell", () => {
  beforeEach(() => {
    useUi.setState({ sidebarCollapsed: false });
    act(() => {
      useActive.setState({
        activeOrgId: ORG_A.id,
        activeProjectId: PROJ_A1.id,
      });
    });
    stubWorkspace();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("renders the workspace nav links", async () => {
    renderWithProviders(<AppShell />);
    await waitFor(() => {
      expect(screen.getByText("Agents")).toBeInTheDocument();
    });
    for (const label of [
      "Policies",
      "Graph",
      "Playground",
      "Audit",
      "API keys",
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("collapsing the sidebar hides the nav labels", async () => {
    renderWithProviders(<AppShell />);
    await waitFor(() => expect(screen.getByText("Agents")).toBeInTheDocument());

    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Collapse sidebar" }));

    expect(useUi.getState().sidebarCollapsed).toBe(true);
    // Labels drop out in icon-only mode; the toggle flips to "Expand".
    expect(screen.queryByText("Agents")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Expand sidebar" }),
    ).toBeInTheDocument();
  });

  it("Cmd/Ctrl-B toggles the sidebar", async () => {
    renderWithProviders(<AppShell />);
    await waitFor(() => expect(screen.getByText("Agents")).toBeInTheDocument());

    act(() => {
      fireEvent.keyDown(window, { key: "b", metaKey: true });
    });
    expect(useUi.getState().sidebarCollapsed).toBe(true);

    act(() => {
      fireEvent.keyDown(window, { key: "B", ctrlKey: true });
    });
    expect(useUi.getState().sidebarCollapsed).toBe(false);
  });

  it("shows the signed-in user and signs out", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AppShell />);

    // Username (email local-part) shows in the account chip.
    await waitFor(() => expect(screen.getByText("dev")).toBeInTheDocument());

    await user.click(screen.getByText("dev"));
    await user.click(await screen.findByText("Log out"));

    await waitFor(() => {
      expect(window.fetch).toHaveBeenCalledWith(
        "/v1/auth/cookie/logout",
        expect.anything(),
      );
    });
  });

  it("bootstraps a first org and project when nothing is active", async () => {
    act(() => {
      useActive.setState({ activeOrgId: null, activeProjectId: null });
    });
    renderWithProviders(<AppShell />);

    await waitFor(() => {
      expect(useActive.getState().activeOrgId).toBe(ORG_A.id);
    });
    await waitFor(() => {
      expect(useActive.getState().activeProjectId).toBe(PROJ_A1.id);
    });
  });

  it("heals a stale active org that the user no longer belongs to", async () => {
    act(() => {
      useActive.setState({ activeOrgId: "ghost-org", activeProjectId: null });
    });
    renderWithProviders(<AppShell />);

    await waitFor(() => {
      expect(useActive.getState().activeOrgId).toBe(ORG_A.id);
    });
  });

  it("heals a stale active project that no longer exists", async () => {
    act(() => {
      useActive.setState({
        activeOrgId: ORG_A.id,
        activeProjectId: "ghost-project",
      });
    });
    renderWithProviders(<AppShell />);

    await waitFor(() => {
      expect(useActive.getState().activeProjectId).toBe(PROJ_A1.id);
    });
  });
});
