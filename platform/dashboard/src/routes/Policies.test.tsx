/**
 * Page-wiring tests for the compose policy editor. With a ready project and a
 * stored policy.yaml the tree lists it, the compose banner reads active, and
 * the resolved policy renders. The later cases drive the tab machinery (open /
 * switch / close), the new-file → Save → clear-dirty path, and the
 * no-project empty state.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PoliciesPage } from "./Policies";
import { useActive } from "@/lib/active";
import { resetAssistant } from "@/lib/assistant";
import { makeQueryClient, renderWithProviders } from "@/test/render";
import { sseResponse } from "@/test/sse";

// The real editor is CodeMirror, which jsdom can't drive by keystroke. Swap it
// for a plain controlled textarea so a test can dirty the buffer. No Policies
// page-wiring test asserts editor internals, so this is safe file-wide.
vi.mock("@/components/PolicyEditor", () => ({
  PolicyEditor: ({
    value,
    onChange,
    readOnly,
  }: {
    value: string;
    onChange: (next: string) => void;
    readOnly?: boolean;
  }) => (
    <textarea
      aria-label="policy editor"
      value={value}
      readOnly={readOnly}
      onChange={(e) => onChange(e.target.value)}
    />
  ),
}));

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
    [`/v1/projects/${PID}/policy/matrix`]: {
      roles: ["default"],
      tools: ["read_ticket"],
      cells: { read_ticket: { default: { mode: "allow", constraint: null } } },
      defaults: { default: { mode: "deny", constraint: null } },
      aliased_default: null,
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
      const route = routes[path];
      // A function answers with its own response (a turn's stream).
      if (typeof route === "function") return route();
      if (path in routes) {
        return new Response(JSON.stringify(route), {
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

  it("opens on the agent a hub link names", async () => {
    renderWithProviders(<PoliciesPage />, {
      initialRoute: "/policies?agent=support_bot",
    });
    await waitFor(() =>
      expect(
        vi
          .mocked(window.fetch)
          .mock.calls.some(([u]) => String(u).includes("agent=support_bot")),
      ).toBe(true),
    );
  });

  it("renders the resolved policy for the entry file's grant", async () => {
    renderWithProviders(<PoliciesPage />);
    await waitFor(() =>
      expect(screen.getByText("read_ticket")).toBeInTheDocument(),
    );
  });

  it("still renders the resolved policy when a lint is an error but not a link error", async () => {
    stubFetch({
      ...baseRoutes(),
      [`/v1/projects/${PID}/policy/check`]: {
        ok: false,
        lints: [
          {
            code: "unknown-guard",
            severity: "error",
            message: "policy governs guard 'secret_redacter'",
          },
        ],
      },
    });
    renderWithProviders(<PoliciesPage />);
    await waitFor(() =>
      expect(screen.getByText("read_ticket")).toBeInTheDocument(),
    );
    expect(screen.queryByText(/doesn't compose/)).not.toBeInTheDocument();
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

    // The new buffer opens in its own tab (the file name lives there now, not
    // in a separate editor header).
    await waitFor(() =>
      expect(screen.getByTitle("extra.yaml")).toBeInTheDocument(),
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

  it("drops a dirty buffer when switching to a cached project (no cross-project write)", async () => {
    const PID2 = "proj-2";
    const P2_POLICY = "tools: { other: { mode: allow } }\n";
    stubFetch({
      ...baseRoutes(),
      "/v1/orgs/org-a/projects": [
        { id: PID, name: "proj-1", org_id: ORG.id },
        { id: PID2, name: "proj-2", org_id: ORG.id },
      ],
      [`/v1/projects/${PID2}/policy-files`]: [file("policy.yaml", P2_POLICY)],
      [`/v1/projects/${PID2}/policy/resolve`]: { roles: {} },
      [`/v1/projects/${PID2}/policy/check`]: { ok: true, lints: [] },
    });

    // Pre-seed project 2's file cache so the later switch to it never hits the
    // loading state that would unmount the pane (the bug's precondition) — no
    // warm-up round-trips, so the test stays deterministic.
    const qc = new QueryClient({
      defaultOptions: {
        queries: { retry: false, staleTime: 30_000 },
        mutations: { retry: false },
      },
    });
    qc.setQueryData(["policy-files", PID2], [file("policy.yaml", P2_POLICY)]);
    renderWithProviders(<PoliciesPage />, { client: qc });
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());

    // Dirty project 1's policy.yaml without saving (one-shot change).
    const editor = screen.getByLabelText("policy editor");
    fireEvent.change(editor, {
      target: { value: "import: [ caps.yaml ]\n# zzAAA_PROJECT_A" },
    });
    expect(editor).toHaveValue("import: [ caps.yaml ]\n# zzAAA_PROJECT_A");

    // Switch to the pre-cached project 2 — the pane stays mounted (no loading),
    // so only the key={projectId} remount stops project 1's buffer from
    // surviving. The editor must show project 2's stored content, not the dirty
    // project-1 text.
    act(() => useActive.setState({ activeProjectId: PID2 }));
    await waitFor(
      () =>
        expect(screen.getByLabelText("policy editor")).toHaveValue(P2_POLICY),
      { timeout: 3000 },
    );
  });
});

describe("PoliciesPage after the assistant saves a change", () => {
  const OLD = "import: [ caps.yaml ]\n";
  const NEW = "import: [ caps.yaml ]\n# lowered by the assistant\n";
  const SUMMARY = "Lower the refund cap";

  function stored(name: string, content: string, hash: string) {
    return { ...file(name, content), text_hash: hash };
  }

  /** The base routes with the assistant on, policy.yaml at `OLD`, and a
   * turn that saves `name` as `NEW` (hash "h-new"), policy.yaml by default.
   * Returns the routes, so a test can change what the files read answers. */
  // A refetch is done once per change id, for the whole module: a fresh id
  // per test keeps one test's change from counting as handled in the next.
  let changes = 0;

  function stubAssistant(name = "policy.yaml"): Record<string, unknown> {
    const changeId = `chg_${++changes}`;
    const routes: Record<string, unknown> = {
      ...baseRoutes(),
      [`/v1/projects/${PID}/policy-files`]: [
        stored("policy.yaml", OLD, "h-old"),
        stored("caps.yaml", "tools: {}\n", "h-caps"),
      ],
      [`/v1/projects/${PID}/assistant/config`]: {
        enabled: true,
        mode: "read_write",
        can_write: true,
        verified: true,
      },
      [`/v1/projects/${PID}/assistant/token`]: {
        token: "t",
        expires_at: "x",
        can_write: true,
      },
    };
    routes[`/v1/assistant/projects/${PID}/turns`] = () => {
      // The save lands on the api before the event reaches the page.
      const before = routes[`/v1/projects/${PID}/policy-files`] as {
        name: string;
      }[];
      routes[`/v1/projects/${PID}/policy-files`] = [
        ...before.filter((f) => f.name !== name),
        stored(name, NEW, "h-new"),
      ];
      return sseResponse(
        {
          type: "CUSTOM",
          name: "change",
          value: {
            change_id: changeId,
            summary: SUMMARY,
            files: [
              {
                name,
                before_hash: before.some((f) => f.name === name)
                  ? "h-old"
                  : null,
                after_hash: "h-new",
              },
            ],
            checks: [{ id: "yaml", status: "pass" }],
          },
        },
        { type: "RUN_FINISHED" },
      );
    };
    stubFetch(routes);
    return routes;
  }

  async function askForChange() {
    await userEvent.type(
      // The tab appears once the assistant's config is read.
      await screen.findByLabelText("Message the agent", {}, { timeout: 5000 }),
      "lower it{Enter}",
    );
  }

  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: ORG.id, activeProjectId: PID });
    });
  });

  afterEach(() => {
    resetAssistant();
    vi.restoreAllMocks();
  });

  it("refetches the files and a clean buffer shows the new text", async () => {
    stubAssistant();
    renderWithProviders(<PoliciesPage />);
    await waitFor(() =>
      expect(screen.getByLabelText("policy editor")).toHaveValue(OLD),
    );
    expect(screen.queryByText("Changed by the assistant")).toBeNull();

    await askForChange();
    await waitFor(() =>
      expect(screen.getByLabelText("policy editor")).toHaveValue(NEW),
    );
    expect(screen.getByText("Changed by the assistant")).toBeInTheDocument();
    expect(
      screen.getByText(SUMMARY, { selector: "[title]" }),
    ).toBeInTheDocument();
  });

  it("keeps unsaved edits and shows no bar on that file", async () => {
    stubAssistant();
    const qc = makeQueryClient();
    renderWithProviders(<PoliciesPage />, { client: qc });
    const editor = await screen.findByLabelText("policy editor");
    await waitFor(() => expect(editor).toHaveValue(OLD));
    fireEvent.change(editor, { target: { value: "# mine\n" } });

    await askForChange();
    // The event has been read: the page holds the files the change left.
    await waitFor(() =>
      expect(
        qc
          .getQueryData<
            { name: string; text_hash: string }[]
          >(["policy-files", PID])
          ?.find((f) => f.name === "policy.yaml")?.text_hash,
      ).toBe("h-new"),
    );
    expect(screen.getByLabelText("policy editor")).toHaveValue("# mine\n");
    expect(screen.queryByText("Changed by the assistant")).toBeNull();
  });

  it("hides the bar once the file no longer holds the assistant's text", async () => {
    stubAssistant();
    const qc = makeQueryClient();
    renderWithProviders(<PoliciesPage />, { client: qc });
    await askForChange();
    await screen.findByText("Changed by the assistant");

    // Saved again since, by someone else: the stored text is no longer the
    // one the change left.
    act(() =>
      qc.setQueryData(
        ["policy-files", PID],
        [stored("policy.yaml", "# theirs\n", "h-theirs")],
      ),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("policy editor")).toHaveValue("# theirs\n"),
    );
    expect(screen.queryByText("Changed by the assistant")).toBeNull();
  });

  it("does not refetch the files again for a change it handled", async () => {
    stubAssistant();
    // One cache for both visits, as in the app.
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
    });
    const filesReads = () =>
      vi
        .mocked(window.fetch)
        .mock.calls.filter(([u]) =>
          String(u).endsWith(`/projects/${PID}/policy-files`),
        ).length;
    const { unmount } = renderWithProviders(<PoliciesPage />, { client: qc });
    await askForChange();
    await screen.findByText("Changed by the assistant");
    const reads = filesReads();
    unmount();

    renderWithProviders(<PoliciesPage />, { client: qc });
    await screen.findByText("Changed by the assistant");
    expect(filesReads()).toBe(reads);
  });

  it("drops an empty new file's buffer once the assistant saves it", async () => {
    stubAssistant("extra.yaml");
    renderWithProviders(<PoliciesPage />);
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: /new file/i }));
    await userEvent.type(
      screen.getByPlaceholderText(/e\.g\./i),
      "extra.yaml{Enter}",
    );
    await waitFor(() =>
      expect(screen.getByLabelText("policy editor")).toHaveValue(""),
    );

    await askForChange();
    await waitFor(() =>
      expect(screen.getByLabelText("policy editor")).toHaveValue(NEW),
    );
    // Saved, so not unsaved: back on the tab, the stored text shows.
    await waitFor(() =>
      expect(screen.queryByTitle("Unsaved changes")).toBeNull(),
    );
    expect(screen.getByText("Changed by the assistant")).toBeInTheDocument();
  });

  /** Opens a new file from the tree, its name set in one change. */
  async function newFile(name: string) {
    fireEvent.click(screen.getByRole("button", { name: /new file/i }));
    const input = screen.getByPlaceholderText(/e\.g\./i);
    fireEvent.change(input, { target: { value: name } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByTitle(name)).toBeInTheDocument());
  }

  it("drops an empty new file's buffer saved while another tab shows", async () => {
    stubAssistant("extra.yaml");
    renderWithProviders(<PoliciesPage />);
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());
    await newFile("extra.yaml");
    await userEvent.click(screen.getByText("policy.yaml"));
    await waitFor(() =>
      expect(screen.getByLabelText("policy editor")).toHaveValue(OLD),
    );

    await askForChange();
    await waitFor(() =>
      expect(screen.queryByTitle("Unsaved changes")).toBeNull(),
    );
    // The editor's tab: the change card names the file too.
    await userEvent.click(screen.getAllByTitle("extra.yaml")[0]);
    await waitFor(() =>
      expect(screen.getByLabelText("policy editor")).toHaveValue(NEW),
    );
  });

  it("keeps a new file's buffer the user emptied after typing", async () => {
    stubAssistant("extra.yaml");
    const qc = makeQueryClient();
    renderWithProviders(<PoliciesPage />, { client: qc });
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());
    await newFile("extra.yaml");
    const editor = screen.getByLabelText("policy editor");
    fireEvent.change(editor, { target: { value: "x" } });
    fireEvent.change(editor, { target: { value: "" } });

    await askForChange();
    // The event has been read: the page holds the file the change created.
    await waitFor(() =>
      expect(
        qc
          .getQueryData<{ name: string }[]>(["policy-files", PID])
          ?.some((f) => f.name === "extra.yaml"),
      ).toBe(true),
    );
    // Typed into, so the user's: still empty and unsaved over the stored file.
    expect(screen.getByLabelText("policy editor")).toHaveValue("");
    expect(screen.getAllByTitle("Unsaved changes").length).toBeGreaterThan(0);
  });

  it("keeps a typed new file's draft when it is opened again by New file", async () => {
    stubAssistant("extra.yaml");
    const qc = makeQueryClient();
    renderWithProviders(<PoliciesPage />, { client: qc });
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument());
    await newFile("extra.yaml");
    fireEvent.change(screen.getByLabelText("policy editor"), {
      target: { value: "mine" },
    });
    fireEvent.click(
      within(screen.getByTitle("extra.yaml")).getByTitle("Close tab"),
    );
    await waitFor(() => expect(screen.queryByTitle("extra.yaml")).toBeNull());
    await newFile("extra.yaml");
    expect(screen.getByLabelText("policy editor")).toHaveValue("mine");

    await askForChange();
    await waitFor(() =>
      expect(
        qc
          .getQueryData<{ name: string }[]>(["policy-files", PID])
          ?.some((f) => f.name === "extra.yaml"),
      ).toBe(true),
    );
    // Still the page's draft: the tab keeps its unsaved mark.
    expect(
      within(screen.getAllByTitle("extra.yaml")[0]).getByTitle(
        "Unsaved changes",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("policy editor")).toHaveValue("mine");
  });

  it("closes the bar when dismissed", async () => {
    stubAssistant();
    renderWithProviders(<PoliciesPage />);
    await askForChange();
    await screen.findByText("Changed by the assistant");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Changed by the assistant")).toBeNull();
  });
});
