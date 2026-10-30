/**
 * Tests for the /agents page — the agent list (each row links into the
 * hub, 24h volume, the legacy ?agent= redirect) and ManifestView's skills
 * and guards sections, whose point is the distinction between resources a
 * framework did not enumerate and a skill that ships none.
 */

import { act, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Route, Routes, useParams } from "react-router-dom";

import { useActive } from "@/lib/active";
import type {
  AgentManifestView,
  GuardDefinition,
  SkillDefinition,
} from "@/lib/api";
import { AgentsPage, ManifestView } from "@/routes/Agents";
import { renderWithProviders } from "@/test/render";

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

function skill(overrides: Partial<SkillDefinition> = {}): SkillDefinition {
  return {
    name: "pdf_filling",
    description: "Fill a PDF form from structured data.",
    source: null,
    resources: null,
    allowed_tools: [],
    additional_tools: [],
    content_hash: null,
    ...overrides,
  };
}

function guard(overrides: Partial<GuardDefinition> = {}): GuardDefinition {
  return {
    name: "secret_guard",
    position: "before",
    tool_names: null,
    observe: false,
    kind: "official",
    plugin_id: "secret_guard",
    ...overrides,
  };
}

function withSkills(skills: SkillDefinition[] | null) {
  return { ...MANIFEST, manifest: { ...MANIFEST.manifest, skills } };
}

function withGuards(guards: GuardDefinition[] | null) {
  return { ...MANIFEST, manifest: { ...MANIFEST.manifest, guards } };
}

function stubFetch(role: string, manifest: unknown = MANIFEST) {
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
              role,
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
        case `/v1/projects/${PROJECT}/policy-files`:
          return json([
            {
              name: "policy.yaml",
              content: "",
              content_hash: "h",
              updated_at: "2026-01-01T00:00:00Z",
            },
          ]);
        case `/v1/projects/${PROJECT}/policy/matrix`:
          return json({
            roles: ["guest", "support"],
            tools: ["refund"],
            cells: {
              refund: {
                guest: { mode: "deny", constraint: null },
                support: { mode: "allow", constraint: "args.amount <= 50" },
              },
              // An unlisted manifest tool: the server's fallback cells.
              ...(url.searchParams.getAll("tool").includes("lookup") && {
                lookup: {
                  guest: { mode: "deny", constraint: null },
                  support: { mode: "allow", constraint: "user.tier == 1" },
                },
              }),
            },
            defaults: {
              guest: { mode: "deny", constraint: null },
              support: { mode: "allow", constraint: "user.tier == 1" },
            },
            aliased_default: null,
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
    stubFetch("admin");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    const link = await screen.findByRole("link", { name: "support_bot" });
    expect(link).toHaveAttribute("href", "/agents/support_bot");
  });

  it("leads with a portfolio KPI strip from the 24h summary", async () => {
    stubFetch("admin");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    // Await a summary-derived value (the labels render before it loads):
    // 4 of 15 denied → the rate rides on the Denied tile.
    expect(await screen.findByText(/26\.7%/)).toBeInTheDocument();
    // Strip labels are distinct from the row's lowercase "calls 24h"/"denied".
    expect(screen.getByText("Calls · 24h")).toBeInTheDocument();
    expect(screen.getByText("across all agents")).toBeInTheDocument();
    expect(screen.getByText("Denied · 24h")).toBeInTheDocument();
    expect(screen.getByText("Pending approvals")).toBeInTheDocument();
  });

  it("shows each agent's 24h call and denial volume on its row", async () => {
    stubFetch("admin");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    const row = await screen.findByRole("link", { name: "support_bot" });
    expect(within(row).getByText("15")).toBeInTheDocument();
    expect(within(row).getByText("calls 24h")).toBeInTheDocument();
    expect(within(row).getByText("4")).toBeInTheDocument();
    expect(within(row).getByText("denied")).toBeInTheDocument();
  });

  it("sends a legacy ?agent= deep link to the agent's hub", async () => {
    stubFetch("admin");
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
    stubFetch("admin", "error");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    expect(
      await screen.findByText("Couldn't load agents."),
    ).toBeInTheDocument();
    expect(screen.queryByText("No agents yet.")).not.toBeInTheDocument();
  });

  it("says there are no agents when the list is empty", async () => {
    stubFetch("admin", "empty");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
    expect(await screen.findByText(/No agents yet/)).toBeInTheDocument();
  });
});

describe("AgentsPage — skills section", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function renderWithSkills(skills: SkillDefinition[] | null) {
    renderWithProviders(
      <ManifestView agent={withSkills(skills) as AgentManifestView} />,
    );
    await screen.findByText("Skills");
  }

  it("renders a skills section with the count", async () => {
    await renderWithSkills([skill(), skill({ name: "invoicing" })]);

    const header = screen.getByText("Skills").parentElement as HTMLElement;
    expect(within(header).getByText("2")).toBeInTheDocument();
  });

  it("renders each skill name and description", async () => {
    await renderWithSkills([skill()]);

    expect(screen.getByText("pdf_filling")).toBeInTheDocument();
    expect(
      screen.getByText("Fill a PDF form from structured data."),
    ).toBeInTheDocument();
  });

  it("shows a scripts badge when the skill ships scripts", async () => {
    await renderWithSkills([
      skill({
        resources: {
          references: [],
          assets: [],
          scripts: ["fill.py", "flatten.py"],
        },
      }),
    ]);

    expect(screen.getByText("2 scripts")).toBeInTheDocument();
  });

  it("omits the scripts badge when there are none", async () => {
    await renderWithSkills([
      skill({ resources: { references: ["a.md"], assets: [], scripts: [] } }),
    ]);

    expect(screen.queryByText(/\bscripts?\b/)).toBeNull();
  });

  it("shows an additional-tools badge", async () => {
    await renderWithSkills([skill({ additional_tools: ["web_search"] })]);

    expect(screen.getByText("+1 tool")).toBeInTheDocument();
  });

  it("pluralises the badges correctly", async () => {
    await renderWithSkills([
      skill({
        resources: { references: [], assets: [], scripts: ["one.py"] },
        additional_tools: ["web_search", "bash"],
      }),
    ]);

    expect(screen.getByText("1 script")).toBeInTheDocument();
    expect(screen.getByText("+2 tools")).toBeInTheDocument();
  });

  // Paired with the next test: either alone passes for the wrong reason.
  it("says resources are not enumerated when resources is null", async () => {
    await renderWithSkills([skill({ resources: null })]);

    expect(
      screen.getByText("Resources not enumerated by this framework."),
    ).toBeInTheDocument();
    expect(screen.queryByText("No resources.")).toBeNull();
  });

  it("says there are no resources when enumerated and empty", async () => {
    await renderWithSkills([
      skill({ resources: { references: [], assets: [], scripts: [] } }),
    ]);

    expect(screen.getByText("No resources.")).toBeInTheDocument();
    expect(
      screen.queryByText("Resources not enumerated by this framework."),
    ).toBeNull();
  });

  // Paired with the next test, mirroring the resources guard one level down:
  // every agent on this branch serialises skills: null, so collapsing it into
  // [] would make the false assertion the only thing the section can show.
  it("says skills were not enumerated when skills is null", async () => {
    await renderWithSkills(null);

    expect(
      screen.getByText("Skills not enumerated at registration."),
    ).toBeInTheDocument();
    expect(screen.queryByText("No skills declared.")).toBeNull();
  });

  it("says no skills are declared when the manifest enumerates none", async () => {
    await renderWithSkills([]);

    expect(screen.getByText("No skills declared.")).toBeInTheDocument();
    expect(
      screen.queryByText("Skills not enumerated at registration."),
    ).toBeNull();
  });

  it("omits the count badge when skills is null", async () => {
    await renderWithSkills(null);

    const header = screen.getByText("Skills").parentElement as HTMLElement;
    expect(within(header).queryByText("0")).toBeNull();
  });

  it("shows a zero count when the manifest enumerates no skills", async () => {
    await renderWithSkills([]);

    const header = screen.getByText("Skills").parentElement as HTMLElement;
    expect(within(header).getByText("0")).toBeInTheDocument();
  });

  it("renders the unregistered state", async () => {
    renderWithProviders(
      <ManifestView
        agent={
          {
            ...MANIFEST,
            manifest: null,
            version: null,
            content_hash: null,
          } as AgentManifestView
        }
      />,
    );

    await screen.findByText("Skills");
    expect(
      screen.getAllByText(
        "Agent not registered yet — run `hexgate register` to populate.",
      ).length,
    ).toBeGreaterThan(0);
  });
});

describe("AgentsPage — guards section", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  async function renderWithGuards(guards: GuardDefinition[] | null) {
    renderWithProviders(
      <ManifestView agent={withGuards(guards) as AgentManifestView} />,
    );
    await screen.findByText("Guards");
  }

  it("renders a guards section with the count", async () => {
    await renderWithGuards([guard(), guard({ name: "secret_watch" })]);

    const header = screen.getByText("Guards").parentElement as HTMLElement;
    expect(within(header).getByText("2")).toBeInTheDocument();
  });

  it("shows each guard's name, position, and tool scope", async () => {
    await renderWithGuards([
      guard({ name: "secret_guard", tool_names: ["send_update"] }),
    ]);

    expect(screen.getByText("secret_guard")).toBeInTheDocument();
    expect(screen.getByText("before")).toBeInTheDocument();
    expect(screen.getByText(/scoped to send_update/)).toBeInTheDocument();
  });

  it("labels a global, observe-only guard", async () => {
    await renderWithGuards([
      guard({ name: "secret_watch", position: "after", observe: true }),
    ]);

    expect(screen.getByText("observe")).toBeInTheDocument();
    expect(screen.getByText(/every tool/)).toBeInTheDocument();
    expect(screen.getByText(/Runs on the tool result/)).toBeInTheDocument();
  });

  it("says no guards when none are declared", async () => {
    await renderWithGuards([]);

    expect(screen.getByText("No guards declared.")).toBeInTheDocument();
  });
});
