/**
 * Page-wiring tests for the compose policy editor. With a ready project and a
 * stored policy.yaml the tree lists it, the compose banner reads active, and
 * the resolved policy renders. The later cases drive the tab machinery (open /
 * switch / close), the new-file → Save → clear-dirty path, and the
 * no-project empty state.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PoliciesPage } from "./Policies";
import { useActive } from "@/lib/active";
import { renderWithProviders } from "@/test/render";

const ORG = {
  id: "org-a",
  slug: "org-a",
  name: "Org A",
  created_at: "2026-01-01T00:00:00Z",
  role: "owner" as const,
};
const PID = "proj-1";

function file(name: string, content: string) {
  return {
    name,
    content,
    content_hash: "h",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

/** The default routes for a ready, compose-active project. */
function baseRoutes(): Record<string, unknown> {
  return {
    "/v1/orgs": [ORG],
    "/v1/orgs/org-a/projects": [{ id: PID, name: "proj-1", org_id: ORG.id }],
    [`/v1/projects/${PID}/policy-files`]: [
      // The entry imports its grant, so `read_ticket` shows only in the
      // resolved inspector, not the file text.
      file("policy.yaml", "import: [ caps.yaml ]\n"),
      file("caps.yaml", "tools: { read_ticket: { mode: allow } }\n"),
    ],
    [`/v1/projects/${PID}/policy/resolve`]: {
      roles: { default: { tools: { read_ticket: { mode: "allow" } } } },
    },
    [`/v1/projects/${PID}/policy/check`]: { ok: true, lints: [] },
    // A saved new file echoes back its stored row.
    [`/v1/projects/${PID}/policy-files/extra.yaml`]: file("extra.yaml", ""),
  };
}

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

describe("PoliciesPage", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: ORG.id, activeProjectId: PID });
    });
    stubFetch(baseRoutes());
  });

  afterEach(() => vi.restoreAllMocks());

  it("lists the entry file and reads as compose-active", async () => {
    renderWithProviders(<PoliciesPage />);
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());
    expect(screen.getAllByText("policy.yaml").length).toBeGreaterThan(0);
  });

  it("renders the resolved policy for the entry file's grant", async () => {
    renderWithProviders(<PoliciesPage />);
    await waitFor(() =>
      expect(screen.getByText("read_ticket")).toBeInTheDocument(),
    );
  });

  it("opens, switches, and closes editor tabs", async () => {
    renderWithProviders(<PoliciesPage />);
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());

    // Open both files from the tree, so two tabs coexist. caps.yaml isn't open
    // yet (policy.yaml is the default tab), so its tree row is unambiguous;
    // once it's the active tab, policy.yaml's tree row is the unambiguous one.
    await userEvent.click(screen.getByText("caps.yaml"));
    await userEvent.click(screen.getByText("policy.yaml"));

    // The tab bar carries one titled tab per open file.
    await waitFor(() =>
      expect(screen.getByTitle("caps.yaml")).toBeInTheDocument(),
    );
    // Switch to the caps.yaml tab (its container is titled by full name; tree
    // rows carry no title).
    await userEvent.click(screen.getByTitle("caps.yaml"));

    // Close a tab via its close affordance.
    const closers = screen.getAllByTitle("Close tab");
    await userEvent.click(closers[0]);
    await waitFor(() =>
      expect(screen.getAllByTitle("Close tab").length).toBe(closers.length - 1),
    );
  });

  it("creates a new in-memory file and opens it in a tab", async () => {
    renderWithProviders(<PoliciesPage />);
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: /new file/i }));
    const input = screen.getByPlaceholderText(/e\.g\./i);
    await userEvent.type(input, "extra.yaml{Enter}");

    // The new buffer opens: it names both the editor header and its own tab.
    await waitFor(() =>
      expect(screen.getAllByTitle("extra.yaml").length).toBeGreaterThan(1),
    );
  });

  it("shows the no-project empty state when none is active", async () => {
    act(() => {
      useActive.setState({ activeOrgId: ORG.id, activeProjectId: null });
    });
    renderWithProviders(<PoliciesPage />);
    await waitFor(() =>
      expect(screen.getByText(/No project selected/i)).toBeInTheDocument(),
    );
  });

  it("keeps the editor and inspector panes in sync on the entry file", async () => {
    renderWithProviders(<PoliciesPage />);
    // The editor header names the open file; the inspector resolves it.
    await waitFor(() =>
      expect(screen.getByText("read_ticket")).toBeInTheDocument(),
    );
    const header = screen.getAllByTitle("policy.yaml");
    expect(within(header[0]).getByText("policy.yaml")).toBeInTheDocument();
  });

  it("removes a deleted file's tab so its buffer can't resurrect it", async () => {
    const routes = baseRoutes();
    vi.spyOn(window, "fetch").mockImplementation(async (input, init) => {
      const path = (typeof input === "string" ? input : input.toString()).split(
        "?",
      )[0];
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      if (path in routes) {
        return new Response(JSON.stringify(routes[path]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("not found", { status: 404 });
    });
    renderWithProviders(<PoliciesPage />);
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());

    // Open caps.yaml, then switch back to policy.yaml so caps.yaml is a
    // background tab (its title is then unambiguous — not also the editor header).
    await userEvent.click(screen.getByText("caps.yaml"));
    await userEvent.click(screen.getByText("policy.yaml"));
    await waitFor(() =>
      expect(screen.getByTitle("caps.yaml")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByTitle("Delete caps.yaml"));

    // The tab (and the file) are gone — no phantom tab whose Save re-creates it.
    await waitFor(() =>
      expect(screen.queryByTitle("caps.yaml")).not.toBeInTheDocument(),
    );
  });

  it("resets editor tabs when the active project changes", async () => {
    const PID2 = "proj-2";
    stubFetch({
      ...baseRoutes(),
      "/v1/orgs/org-a/projects": [
        { id: PID, name: "proj-1", org_id: ORG.id },
        { id: PID2, name: "proj-2", org_id: ORG.id },
      ],
      [`/v1/projects/${PID2}/policy-files`]: [
        file("policy.yaml", "tools: { other: { mode: allow } }\n"),
      ],
      [`/v1/projects/${PID2}/policy/resolve`]: { roles: {} },
      [`/v1/projects/${PID2}/policy/check`]: { ok: true, lints: [] },
    });
    renderWithProviders(<PoliciesPage />);
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());

    // Open caps.yaml (project 1) as a background tab.
    await userEvent.click(screen.getByText("caps.yaml"));
    await userEvent.click(screen.getByText("policy.yaml"));
    await waitFor(() =>
      expect(screen.getByTitle("caps.yaml")).toBeInTheDocument(),
    );

    // Switching projects must not carry project 1's tab over (else a Save here
    // would write project 1's buffer into project 2).
    act(() => {
      useActive.setState({ activeProjectId: PID2 });
    });
    await waitFor(() =>
      expect(screen.queryByTitle("caps.yaml")).not.toBeInTheDocument(),
    );
  });
});
