/**
 * Smoke tests for /orgs/:orgId/settings.
 *
 * Two load-bearing assertions:
 *
 *   1. Plain members see the view-only banner + no Save button.
 *   2. Owners see the form enabled + Save button.
 *
 * Last-owner / leave-org flows would require a deeper backend stub
 * (the auth /users/me + the DELETE handler) — covered by the manual
 * walkthrough + Phase 4's backend tests, not duplicated here.
 */

import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router-dom";

import { OrgSettingsPage } from "@/routes/OrgSettings";
import { renderWithProviders } from "@/test/render";

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

/** Routed test render — OrgSettings reads :orgId from the URL via
 * useParams, so the MemoryRouter needs the route shape mounted. */
function renderSettings(orgId: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/orgs/:orgId/settings" element={<OrgSettingsPage />} />
    </Routes>,
    { initialRoute: `/orgs/${orgId}/settings` },
  );
}

describe("OrgSettingsPage", () => {
  beforeEach(() => {
    // No localStorage state needed — the page reads from useOrgs(),
    // not the active store, to find the org details.
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders view-only mode for plain members (no save button)", async () => {
    stubFetch({
      "/v1/orgs": [
        {
          id: "org-1",
          slug: "acme",
          name: "Acme Inc",
          created_at: "2026-01-01T00:00:00Z",
          role: "member",
        },
      ],
      "/v1/users/me": {
        id: "me",
        email: "plain@example.com",
        is_active: true,
        is_superuser: false,
        is_verified: true,
      },
    });

    renderSettings("org-1");

    await waitFor(() => {
      expect(screen.getByText(/View-only/i)).toBeInTheDocument();
    });
    // Save button isn't rendered for plain members.
    expect(screen.queryByText("Save changes")).not.toBeInTheDocument();
    // "Leave organization" is always available — members can leave.
    expect(screen.getByText("Leave organization")).toBeInTheDocument();
  });

  it("renders editable form + Save button for owners", async () => {
    stubFetch({
      "/v1/orgs": [
        {
          id: "org-1",
          slug: "acme",
          name: "Acme Inc",
          created_at: "2026-01-01T00:00:00Z",
          role: "owner",
        },
      ],
      "/v1/users/me": {
        id: "me",
        email: "owner@example.com",
        is_active: true,
        is_superuser: false,
        is_verified: true,
      },
    });

    renderSettings("org-1");

    await waitFor(() => {
      expect(screen.getByLabelText("Name")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("Name")).not.toBeDisabled();
    expect(screen.getByLabelText("Slug")).not.toBeDisabled();
    expect(screen.getByText("Save changes")).toBeInTheDocument();
    // The view-only banner shouldn't show.
    expect(screen.queryByText(/View-only/i)).not.toBeInTheDocument();
  });

  it("shows \"not found\" when the org isn't in the user's list", async () => {
    // Empty list — orgsQuery resolves, but no org matches the URL id.
    stubFetch({
      "/v1/orgs": [],
      "/v1/users/me": {
        id: "me",
        email: "wanderer@example.com",
        is_active: true,
        is_superuser: false,
        is_verified: true,
      },
    });

    renderSettings("nonexistent-org");

    await waitFor(() => {
      expect(screen.getByText(/Organization not found/i)).toBeInTheDocument();
    });
  });
});
