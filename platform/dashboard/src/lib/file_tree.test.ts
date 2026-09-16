/**
 * Pure tests for the compose file-tree builder + lint line anchoring. These
 * back the left pane (flat file names rendered as a filesystem) and the inline
 * gutter markers, so they're worth pinning without a DOM.
 */

import { describe, expect, it } from "vitest";

import type { PolicyFileRead, PolicyLint } from "./api";
import { baseName, buildFileTree, lintToLine } from "./file_tree";

function file(name: string): PolicyFileRead {
  return {
    name,
    content: "",
    content_hash: "h",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

describe("buildFileTree", () => {
  it("returns an empty tree for no files", () => {
    expect(buildFileTree([])).toEqual([]);
  });

  it("pins the entry file first at the root", () => {
    const tree = buildFileTree([file("aaa.yaml"), file("policy.yaml")]);
    expect(tree[0]).toMatchObject({ type: "file", full: "policy.yaml" });
  });

  it("nests slash-separated names into folders", () => {
    const tree = buildFileTree([
      file("policy.yaml"),
      file("caps/refunds.yaml"),
    ]);
    const folder = tree.find((n) => n.type === "folder");
    expect(folder).toMatchObject({
      type: "folder",
      name: "caps",
      prefix: "caps",
    });
    expect(folder?.type === "folder" && folder.children[0]).toMatchObject({
      type: "file",
      name: "refunds.yaml",
      full: "caps/refunds.yaml",
    });
  });

  it("sorts folders before files and each group alphabetically", () => {
    const tree = buildFileTree([
      file("zeta.yaml"),
      file("alpha.yaml"),
      file("caps/x.yaml"),
    ]);
    expect(tree.map((n) => (n.type === "folder" ? n.name : n.full))).toEqual([
      "caps",
      "alpha.yaml",
      "zeta.yaml",
    ]);
  });
});

describe("baseName", () => {
  it("returns the last path segment", () => {
    expect(baseName("caps/refunds.yaml")).toBe("refunds.yaml");
    expect(baseName("policy.yaml")).toBe("policy.yaml");
  });
});

describe("lintToLine", () => {
  const content =
    "tools:\n  refund: { mode: allow }\n  view: { mode: allow }\n";

  it("anchors a tool-scoped lint to its indented key line", () => {
    const lint = { tool: "view" } as PolicyLint;
    expect(lintToLine(content, lint)).toBe(3);
  });

  it("returns null for a lint with no tool", () => {
    expect(lintToLine(content, { tool: null } as PolicyLint)).toBeNull();
  });
});
