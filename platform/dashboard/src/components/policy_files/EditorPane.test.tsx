/**
 * EditorPane tests — the load-bearing bits: the unsaved badge, and that a
 * failed Save (the server's 422 invalid / 409 breaks-resolution) surfaces
 * inline in the diagnostics bar, not just as a transient toast.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PolicyFileRead } from "@/lib/api";
import { EditorPane } from "./EditorPane";
import { renderWithProviders } from "@/test/render";

function file(name: string, content: string): PolicyFileRead {
  return {
    name,
    content,
    content_hash: "h",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const FILES = [file("policy.yaml", "tools: {}\n")];
const noop = () => undefined;

afterEach(() => vi.restoreAllMocks());

function stubStatus(status: number, detail: unknown) {
  vi.spyOn(window, "fetch").mockImplementation(async () =>
    status === 204
      ? new Response(null, { status })
      : new Response(JSON.stringify(detail), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
  );
}

function renderPane(drafts: Record<string, string> = {}) {
  return renderWithProviders(
    <EditorPane
      projectId="p1"
      name="policy.yaml"
      files={FILES}
      lints={[]}
      canManage
      onDraftChange={noop}
      drafts={drafts}
      onPersist={noop}
    />,
  );
}

describe("EditorPane", () => {
  it("shows the file name and no unsaved badge when clean", () => {
    renderPane();
    expect(screen.getByText("policy.yaml")).toBeInTheDocument();
    expect(screen.queryByText("unsaved")).not.toBeInTheDocument();
  });

  it("marks the buffer unsaved when a draft differs from stored", () => {
    renderPane({ "policy.yaml": "tools: { a: { mode: allow } }\n" });
    expect(screen.getByText("unsaved")).toBeInTheDocument();
  });

  it("surfaces a 422 save error inline in the diagnostics bar", async () => {
    stubStatus(422, { detail: "invalid compose document" });
    renderPane({ "policy.yaml": "not: valid: compose\n" });

    await userEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(screen.getByText("invalid compose document")).toBeInTheDocument(),
    );
  });

  it("surfaces a 409 breaks-resolution error inline", async () => {
    stubStatus(409, { detail: "would break resolution for agent 'bot'" });
    renderPane({ "policy.yaml": "import: [ missing.yaml ]\n" });

    await userEvent.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() =>
      expect(screen.getByText(/would break resolution/)).toBeInTheDocument(),
    );
  });
});
