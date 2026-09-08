/**
 * FileTree tests — the flat file list renders as a nested tree with the entry
 * `policy.yaml` pinned, rows select / mark dirty, folders collapse, and the
 * new-file + delete affordances behave (including the "already exists → open"
 * shortcut and the entry file having no delete).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";

import type { PolicyFileRead } from "@/lib/api";
import { FileTree } from "./FileTree";
import { renderWithProviders } from "@/test/render";

function file(name: string): PolicyFileRead {
  return { name, content: "x", content_hash: "h", updated_at: "t" };
}

const FILES = [file("policy.yaml"), file("caps/refunds.yaml")];

function renderTree(props: Partial<Parameters<typeof FileTree>[0]> = {}) {
  const onSelect = vi.fn();
  const onNewFile = vi.fn();
  renderWithProviders(
    <FileTree
      files={FILES}
      selected="policy.yaml"
      onSelect={onSelect}
      onNewFile={onNewFile}
      projectId="p1"
      canManage
      dirtyKeys={new Set(["caps/refunds.yaml"])}
      {...props}
    />,
  );
  return { onSelect, onNewFile };
}

afterEach(() => vi.restoreAllMocks());

describe("FileTree", () => {
  it("nests folders and pins the entry file, showing a dirty dot", () => {
    renderTree();
    expect(screen.getByText("policy.yaml")).toBeInTheDocument();
    expect(screen.getByText("caps")).toBeInTheDocument(); // derived folder
    expect(screen.getByText("refunds.yaml")).toBeInTheDocument();
    // The dirty file carries an "Unsaved changes" marker.
    expect(screen.getByTitle("Unsaved changes")).toBeInTheDocument();
  });

  it("selects a file on click", async () => {
    const { onSelect } = renderTree();
    await userEvent.click(screen.getByText("refunds.yaml"));
    expect(onSelect).toHaveBeenCalledWith("caps/refunds.yaml");
  });

  it("collapses a folder, hiding its children", async () => {
    renderTree();
    expect(screen.getByText("refunds.yaml")).toBeInTheDocument();
    await userEvent.click(screen.getByText("caps"));
    expect(screen.queryByText("refunds.yaml")).not.toBeInTheDocument();
  });

  it("adds a new file via the input + Enter", async () => {
    const { onNewFile } = renderTree();
    await userEvent.click(screen.getByRole("button", { name: /new file/i }));
    const input = screen.getByPlaceholderText(/e\.g\./i);
    await userEvent.type(input, "caps/new.yaml{Enter}");
    expect(onNewFile).toHaveBeenCalledWith("caps/new.yaml");
  });

  it("opens an existing file instead of re-creating it", async () => {
    const { onNewFile, onSelect } = renderTree();
    await userEvent.click(screen.getByRole("button", { name: /new file/i }));
    const input = screen.getByPlaceholderText(/e\.g\./i);
    await userEvent.type(input, "policy.yaml{Enter}");
    expect(onNewFile).not.toHaveBeenCalled();
    expect(onSelect).toHaveBeenCalledWith("policy.yaml");
  });

  it("deletes a stored non-entry file", async () => {
    const spy = vi
      .spyOn(window, "fetch")
      .mockImplementation(async () => new Response(null, { status: 204 }));
    renderTree();
    await userEvent.click(screen.getByTitle("Delete caps/refunds.yaml"));
    await waitFor(() => expect(spy).toHaveBeenCalled());
    const url = String(spy.mock.calls[0][0]);
    expect(url).toContain("/policy-files/caps/refunds.yaml");
    expect(spy.mock.calls[0][1]?.method).toBe("DELETE");
  });

  it("gives the entry file no delete button", () => {
    renderTree();
    expect(screen.queryByTitle("Delete policy.yaml")).not.toBeInTheDocument();
  });

  it("hides new-file + delete when the caller can't manage", () => {
    renderTree({ canManage: false });
    expect(
      screen.queryByRole("button", { name: /new file/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTitle("Delete caps/refunds.yaml"),
    ).not.toBeInTheDocument();
  });

  it("shows the empty state with no files", () => {
    renderTree({ files: [] });
    expect(screen.getByText(/No files yet/i)).toBeInTheDocument();
  });

  it("surfaces a delete error via a toast", async () => {
    const errorToast = vi.spyOn(toast, "error").mockReturnValue("t");
    vi.spyOn(window, "fetch").mockImplementation(
      async () =>
        new Response(JSON.stringify({ detail: "still imported" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
    );
    renderTree();
    await userEvent.click(screen.getByTitle("Delete caps/refunds.yaml"));
    await waitFor(() =>
      expect(errorToast).toHaveBeenCalledWith("still imported"),
    );
  });
});
