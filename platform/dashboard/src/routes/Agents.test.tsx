/**
 * Tests for the /agents page — the "ban agent" cross-page tie-in (§9.6)
 * and the skills section, whose point is the distinction between resources
 * a framework did not enumerate and a skill that ships none.
 */

import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useActive } from "@/lib/active";
import type { SkillDefinition } from "@/lib/api";
import { AgentsPage } from "@/routes/Agents";
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

function withSkills(skills: SkillDefinition[] | null) {
  return { ...MANIFEST, manifest: { ...MANIFEST.manifest, skills } };
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
          return json([manifest]);
        default:
          return new Response("not found", { status: 404 });
      }
    },
  );
}

describe("AgentsPage — ban tie-in", () => {
  beforeEach(() => {
    act(() => {
      useActive.setState({ activeOrgId: "org-1", activeProjectId: PROJECT });
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows an admin a ban-agent link pointing at the prefilled /bans route", async () => {
    stubFetch("admin");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });

    const link = await screen.findByRole("link", { name: /ban agent/i });
    expect(link).toHaveAttribute("href", "/bans?ban_agent=support_bot");
  });

  it("hides the ban-agent link from a plain member", async () => {
    stubFetch("member");
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });

    // The edit-policy link still renders, proving the header mounted —
    // only the ban affordance is gated away.
    await screen.findByRole("link", { name: /edit policy/i });
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: /ban agent/i })).toBeNull(),
    );
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
    stubFetch("admin", withSkills(skills));
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });
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

  it("renders an empty state when skills is null", async () => {
    await renderWithSkills(null);

    expect(screen.getByText("No skills declared.")).toBeInTheDocument();
  });

  it("renders the unregistered state", async () => {
    stubFetch("admin", {
      ...MANIFEST,
      manifest: null,
      version: null,
      content_hash: null,
    });
    renderWithProviders(<AgentsPage />, { initialRoute: "/agents" });

    await screen.findByText("Skills");
    expect(
      screen.getAllByText(
        "Agent not registered yet — run `hexgate register` to populate.",
      ).length,
    ).toBeGreaterThan(0);
  });
});
