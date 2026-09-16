import { describe, expect, it } from "vitest";

import {
  composeAgentNames,
  referencesImport,
  rewriteImportPath,
} from "./imports";

const ENTRY = `agents:
  support_bot:
    roles:
      default: { import: [ caps/read_only.yaml ] }
      billing:
        import:
          [ caps/read_only.yaml, caps/payments.yaml ]
  billing_bot:
    roles:
      billing: { import: [ caps/payments.yaml ] }
`;

describe("referencesImport", () => {
  it("detects a path used in an import list", () => {
    expect(referencesImport(ENTRY, "caps/payments.yaml")).toBe(true);
  });

  it("is false when the path is absent", () => {
    expect(referencesImport(ENTRY, "caps/missing.yaml")).toBe(false);
  });

  it("does not match a path that is a prefix of a longer one", () => {
    // "caps/read_only" must not match inside "caps/read_only.yaml".
    expect(referencesImport(ENTRY, "caps/read_only")).toBe(false);
  });

  it("ignores a path only mentioned outside an import list", () => {
    // A comment/description referencing the path is not an import.
    const src =
      "tools:\n  x: { mode: allow }  # see caps/refunds.yaml for the cap\n";
    expect(referencesImport(src, "caps/refunds.yaml")).toBe(false);
  });

  it("falls back to a token scan when the file does not parse", () => {
    const broken = "import: [ caps/refunds.yaml ]\n\t: : bad yaml";
    expect(referencesImport(broken, "caps/refunds.yaml")).toBe(true);
  });
});

describe("composeAgentNames", () => {
  it("returns the entry's declared agents, sorted", () => {
    expect(composeAgentNames(ENTRY)).toEqual(["billing_bot", "support_bot"]);
  });

  it("returns [] for a top-level-only policy (no agents block)", () => {
    expect(composeAgentNames("tools:\n  x: { mode: allow }\n")).toEqual([]);
  });

  it("returns [] when the file does not parse", () => {
    expect(composeAgentNames("agents:\n\t: : bad yaml")).toEqual([]);
  });
});

describe("rewriteImportPath", () => {
  it("rewrites every occurrence, inline and multiline", () => {
    const out = rewriteImportPath(
      ENTRY,
      "caps/read_only.yaml",
      "shared/read_only.yaml",
    );
    expect(out).toContain("shared/read_only.yaml");
    expect(out).not.toContain("caps/read_only.yaml");
    // The untouched sibling import is preserved.
    expect(out).toContain("caps/payments.yaml");
  });

  it("leaves a longer path that merely contains the old one intact", () => {
    const src = "import: [ caps/read_only.yaml, caps/read_only.yaml.bak ]";
    const out = rewriteImportPath(src, "caps/read_only.yaml", "x.yaml");
    expect(out).toBe("import: [ x.yaml, caps/read_only.yaml.bak ]");
  });

  it("is a no-op when the path is not present", () => {
    expect(rewriteImportPath(ENTRY, "nope.yaml", "x.yaml")).toBe(ENTRY);
  });
});
