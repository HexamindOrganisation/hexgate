/**
 * Tests for the /connected-apps page.
 *
 * Invariants:
 *   1. Each grant shows its client, scopes in plain words, and times.
 *   2. Each grant shows the projects it covers — not another grant's.
 *   3. Revoke confirms, DELETEs that grant, and its row goes away — also
 *      when the grant was already revoked elsewhere (404).
 *   4. No grants → the empty state; a failed read → the error state.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { toast } from "sonner";

import type { OAuthGrant } from "@/lib/api";
import { ConnectedAppsPage } from "@/routes/ConnectedApps";
import { renderWithProviders } from "@/test/render";

const CLAUDE: OAuthGrant = {
  id: "fam-1",
  client_name: "Claude Code",
  client_id: "https://claude.ai/oauth/claude-code-client-metadata",
  scopes: [
    {
      name: "policy:read",
      description: "Read and check the policies of the projects you choose",
    },
  ],
  projects: [
    { id: "p1", name: "billing", org_id: "o1", org_name: "Acme" },
    { id: "p2", name: "support", org_id: "o1", org_name: "Acme" },
  ],
  created_at: "2026-10-01T10:00:00Z",
  last_used_at: null,
};

const CURSOR: OAuthGrant = {
  ...CLAUDE,
  id: "fam-2",
  client_name: "Cursor",
  client_id: "https://cursor.example:8443/client.json",
  projects: [{ id: "p3", name: "sandbox", org_id: "o2", org_name: "Lab" }],
  last_used_at: "2026-10-02T10:00:00Z",
};

interface Stub {
  grants?: OAuthGrant[];
  listStatus?: number;
  /** The DELETE's status; 404 = already revoked elsewhere. */
  deleteStatus?: number;
}

/** Stubs both grant routes; the DELETE removes the grant from what the next
 * GET returns, like the real endpoint. Returns the DELETEd paths. */
function stubFetch({
  grants = [],
  listStatus = 200,
  deleteStatus = 204,
}: Stub): string[] {
  let live = [...grants];
  const deleted: string[] = [];
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://localhost").pathname;
      if (init?.method === "DELETE") {
        deleted.push(path);
        live = live.filter((g) => path !== `/v1/oauth/grants/${g.id}`);
        return deleteStatus === 204
          ? new Response(null, { status: 204 })
          : new Response(JSON.stringify({ detail: "Not Found" }), {
              status: deleteStatus,
            });
      }
      if (path === "/v1/oauth/grants") {
        return new Response(
          JSON.stringify(listStatus === 200 ? live : { detail: "boom" }),
          {
            status: listStatus,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      return new Response("not found", { status: 404 });
    },
  );
  return deleted;
}

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}));

/** The row of one grant, found by its client name. */
function rowOf(clientName: string): HTMLElement {
  return screen.getByText(clientName).closest("li") as HTMLElement;
}

describe("ConnectedAppsPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    // The sonner mock is a vi.fn, which restoreAllMocks leaves as it was.
    vi.clearAllMocks();
  });

  it("lists each grant with its client, host, scopes and times", async () => {
    stubFetch({ grants: [CLAUDE, CURSOR] });
    renderWithProviders(<ConnectedAppsPage />);

    await waitFor(() =>
      expect(screen.getByText("Claude Code")).toBeInTheDocument(),
    );
    const claude = within(rowOf("Claude Code"));
    expect(claude.getByText("claude.ai")).toBeInTheDocument();
    // The hostname without the port, as the consent page shows it.
    expect(
      within(rowOf("Cursor")).getByText("cursor.example"),
    ).toBeInTheDocument();
    expect(
      claude.getByText(
        "Read and check the policies of the projects you choose",
      ),
    ).toBeInTheDocument();
    // Never renewed: no claim either way about whether it was used.
    expect(claude.queryByText(/renewed/)).not.toBeInTheDocument();
    expect(
      within(rowOf("Cursor")).getByText(/Access last renewed/),
    ).toBeInTheDocument();
  });

  it("shows each grant's own projects", async () => {
    stubFetch({ grants: [CLAUDE, CURSOR] });
    renderWithProviders(<ConnectedAppsPage />);

    await waitFor(() =>
      expect(screen.getByText("Claude Code")).toBeInTheDocument(),
    );
    const claude = within(rowOf("Claude Code"));
    expect(claude.getByText("Acme / billing")).toBeInTheDocument();
    expect(claude.getByText("Acme / support")).toBeInTheDocument();
    expect(claude.queryByText("Lab / sandbox")).not.toBeInTheDocument();
    expect(
      within(rowOf("Cursor")).getByText("Lab / sandbox"),
    ).toBeInTheDocument();
  });

  it("revokes a grant after confirmation and removes its row", async () => {
    const deleted = stubFetch({ grants: [CLAUDE, CURSOR] });
    renderWithProviders(<ConnectedAppsPage />);
    const user = userEvent.setup();

    await waitFor(() =>
      expect(screen.getByText("Claude Code")).toBeInTheDocument(),
    );
    await user.click(
      within(rowOf("Claude Code")).getByRole("button", { name: "Revoke" }),
    );
    // Nothing is sent until the dialog is confirmed.
    expect(deleted).toEqual([]);
    const dialog = await screen.findByRole("dialog");
    // An issued access token outlives the revoke: neither the dialog nor the
    // toast may promise an immediate logout.
    expect(dialog).toHaveTextContent("Access ends within 10 minutes.");
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));

    await waitFor(() =>
      expect(screen.queryByText("Claude Code")).not.toBeInTheDocument(),
    );
    expect(toast.success).toHaveBeenCalledWith(
      "Claude Code was logged out. Access ends within 10 minutes.",
    );
    expect(deleted).toEqual(["/v1/oauth/grants/fam-1"]);
    expect(screen.getByText("Cursor")).toBeInTheDocument();
  });

  it("treats a grant already revoked elsewhere as logged out", async () => {
    stubFetch({ grants: [CLAUDE], deleteStatus: 404 });
    renderWithProviders(<ConnectedAppsPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Revoke" }));

    // The dialog closes as on success, rather than reporting a failure.
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("No connected apps")).toBeInTheDocument();
  });

  it("shows the empty state when no app is connected", async () => {
    stubFetch({ grants: [] });
    renderWithProviders(<ConnectedAppsPage />);

    expect(await screen.findByText("No connected apps")).toBeInTheDocument();
  });

  it("shows the error state when the list cannot be read", async () => {
    stubFetch({ listStatus: 500 });
    renderWithProviders(<ConnectedAppsPage />);

    expect(
      await screen.findByText("Could not load your connected apps."),
    ).toBeInTheDocument();
    expect(screen.queryByText("No connected apps")).not.toBeInTheDocument();
  });
});
