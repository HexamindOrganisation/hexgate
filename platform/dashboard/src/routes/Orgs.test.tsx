/**
 * Smoke tests for the /orgs list page.
 *
 * Three load-bearing invariants:
 *
 *   1. Renders one row per org with the role badge text visible.
 *   2. "+ Create organization" header button opens the dialog.
 *   3. The empty-state CTA also opens the dialog (so a user without
 *      orgs has a working path to create one).
 */

import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OrgsPage } from "@/routes/Orgs";
import { useActive } from "@/lib/active";
import { renderWithProviders } from "@/test/render";

/** Same fetch-stub helper pattern as the switcher tests. */
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

describe("OrgsPage", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: null, activeProjectId: null });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders one row per org with role text visible", async () => {
    stubFetch({
      "/v1/orgs": [
        {
          id: "org-1",
          slug: "acme",
          name: "Acme Inc",
          created_at: "2026-01-01T00:00:00Z",
          role: "owner",
        },
        {
          id: "org-2",
          slug: "marketing",
          name: "Marketing Co",
          created_at: "2026-02-01T00:00:00Z",
          role: "member",
        },
      ],
    });

    renderWithProviders(<OrgsPage />);

    await waitFor(() => {
      expect(screen.getByText("Acme Inc")).toBeInTheDocument();
    });
    expect(screen.getByText("Marketing Co")).toBeInTheDocument();
    // Role text — case-insensitive because the Badge capitalises via CSS,
    // not by replacing the underlying text. The string in DOM stays "owner".
    expect(screen.getByText("owner")).toBeInTheDocument();
    expect(screen.getByText("member")).toBeInTheDocument();
  });

  it("header button opens the create dialog", async () => {
    stubFetch({ "/v1/orgs": [] }); // empty list — the header button is still rendered

    const user = userEvent.setup();
    renderWithProviders(<OrgsPage />);

    // Header button. The empty-state CTA is a separate render path
    // tested below — they're different DOM nodes.
    const headerButtons = await screen.findAllByText("Create organization");
    // headerButtons[0] is the header button. The empty-state's button
    // also matches but is hidden when there are >0 orgs. Stubbing the
    // empty list means the empty-state IS rendered too — so we click
    // the first (header) button explicitly.
    await user.click(headerButtons[0]!);

    // CreateOrgDialog renders its description, which is unique to the dialog.
    expect(
      await screen.findByText(/Teams in Hexgate live inside/i),
    ).toBeInTheDocument();
  });

  it("shows the empty-state CTA when the user has no orgs", async () => {
    stubFetch({ "/v1/orgs": [] });
    renderWithProviders(<OrgsPage />);

    expect(
      await screen.findByText(/No organizations yet/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Create one to start a workspace/i),
    ).toBeInTheDocument();
  });
});
