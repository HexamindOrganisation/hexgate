/** ManifestView's skills and guards sections, whose point is the difference
 * between resources a framework didn't enumerate and a skill that ships none. */

import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ManifestView } from "@/components/agents/ManifestView";
import type {
  AgentManifestView,
  GuardDefinition,
  SkillDefinition,
} from "@/lib/api";
import { renderWithProviders } from "@/test/render";

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

describe("ManifestView — skills section", () => {
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

describe("ManifestView — guards section", () => {
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
