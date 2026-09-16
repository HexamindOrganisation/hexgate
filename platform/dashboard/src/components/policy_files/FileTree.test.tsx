/**
 * FileTree tests — the flat file list renders as a nested tree with the entry
 * `policy.yaml` pinned, rows select / mark dirty, folders collapse, and the
 * new-file + delete affordances behave (including the "already exists → open"
 * shortcut and the entry file having no delete).
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
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
  const onAddFolder = vi.fn();
  const onRemoveFolder = vi.fn();
  const onRename = vi.fn();
  renderWithProviders(
    <FileTree
      files={FILES}
      emptyFolders={[]}
      selected="policy.yaml"
      onSelect={onSelect}
      onNewFile={onNewFile}
      onAddFolder={onAddFolder}
      onRemoveFolder={onRemoveFolder}
      onRename={onRename}
      projectId="p1"
      canManage
      dirtyKeys={new Set(["caps/refunds.yaml"])}
      {...props}
    />,
  );
  return { onSelect, onNewFile, onAddFolder, onRemoveFolder, onRename };
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
    await userEvent.click(screen.getByRole("button", { name: "New file" }));
    const input = screen.getByPlaceholderText(/e\.g\./i);
    await userEvent.type(input, "caps/new.yaml{Enter}");
    expect(onNewFile).toHaveBeenCalledWith("caps/new.yaml");
  });

  it("opens an existing file instead of re-creating it", async () => {
    const { onNewFile, onSelect } = renderTree();
    await userEvent.click(screen.getByRole("button", { name: "New file" }));
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

  it("badges the entry file", () => {
    renderTree();
    expect(screen.getByText("entry")).toBeInTheDocument();
    expect(
      screen.getByTitle("The file the resolver starts from"),
    ).toBeInTheDocument();
  });

  it("creates a folder via the new-folder input", async () => {
    const { onAddFolder } = renderTree();
    await userEvent.click(screen.getByRole("button", { name: "New folder" }));
    await userEvent.type(
      screen.getByLabelText("New folder name"),
      "team_a{Enter}",
    );
    expect(onAddFolder).toHaveBeenCalledWith("team_a");
  });

  it("creates a subfolder via a folder's new-folder button", async () => {
    const { onAddFolder } = renderTree(); // caps/ exists (holds refunds.yaml)
    await userEvent.click(screen.getByTitle("New folder in caps"));
    const input = screen.getByLabelText("New folder name");
    expect(input).toHaveValue(""); // an inline row under caps, leaf name only
    await userEvent.type(input, "archived{Enter}");
    expect(onAddFolder).toHaveBeenCalledWith("caps/archived");
  });

  it("keeps a collapsed parent expanded after adding a subfolder", async () => {
    renderTree(); // caps/ holds refunds.yaml
    await userEvent.click(screen.getByText("caps")); // collapse it
    expect(screen.queryByText("refunds.yaml")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTitle("New folder in caps"));
    await userEvent.type(
      screen.getByLabelText("New folder name"),
      "sub{Enter}",
    );
    // caps stays open after the row commits, so its contents stay visible.
    expect(screen.getByText("refunds.yaml")).toBeInTheDocument();
  });

  it("cancels the new-folder row on Escape without creating one", async () => {
    const { onAddFolder } = renderTree();
    await userEvent.click(screen.getByRole("button", { name: "New folder" }));
    const input = screen.getByLabelText("New folder name");
    await userEvent.type(input, "scratch{Escape}");
    // Escape closes the row (and the trailing blur must not commit it).
    expect(screen.queryByLabelText("New folder name")).not.toBeInTheDocument();
    expect(onAddFolder).not.toHaveBeenCalled();
  });

  it("renders a remembered empty folder and lets it be removed", async () => {
    const { onRemoveFolder } = renderTree({ emptyFolders: ["scratch"] });
    expect(screen.getByText("scratch")).toBeInTheDocument();
    await userEvent.click(screen.getByTitle("Remove empty folder scratch"));
    expect(onRemoveFolder).toHaveBeenCalledWith("scratch");
  });

  it("has no remove button on a folder that holds files", () => {
    renderTree(); // caps/ holds refunds.yaml
    expect(
      screen.queryByTitle("Remove empty folder caps"),
    ).not.toBeInTheDocument();
  });

  it("renames a clean file via the pencil + Enter", async () => {
    // refunds.yaml is clean here (only policy.yaml dirty), so rename is allowed.
    const { onRename } = renderTree({ dirtyKeys: new Set(["policy.yaml"]) });
    await userEvent.click(screen.getByTitle("Rename caps/refunds.yaml"));
    const input = screen.getByLabelText("Rename caps/refunds.yaml");
    await userEvent.clear(input);
    await userEvent.type(input, "caps/refund.yaml{Enter}");
    expect(onRename).toHaveBeenCalledWith(
      "caps/refunds.yaml",
      "caps/refund.yaml",
    );
  });

  it("dispatches rename exactly once on Enter (no blur double-fire)", async () => {
    const { onRename } = renderTree({ dirtyKeys: new Set(["policy.yaml"]) });
    await userEvent.click(screen.getByTitle("Rename caps/refunds.yaml"));
    const input = screen.getByLabelText("Rename caps/refunds.yaml");
    await userEvent.clear(input);
    // Enter commits + unmounts the input; its blur must not fire a 2nd rename.
    await userEvent.type(input, "caps/refund.yaml{Enter}");
    expect(onRename).toHaveBeenCalledTimes(1);
  });

  it("bars rename on the entry file and on dirty files", () => {
    // policy.yaml is entry; caps/refunds.yaml is dirty (default set).
    renderTree();
    expect(screen.queryByTitle("Rename policy.yaml")).not.toBeInTheDocument();
    expect(
      screen.queryByTitle("Rename caps/refunds.yaml"),
    ).not.toBeInTheDocument();
  });

  it("moves a file to the root when dropped on the empty area", async () => {
    const { onRename } = renderTree({ dirtyKeys: new Set(["policy.yaml"]) });
    const data = new Map<string, string>();
    const dataTransfer = {
      setData: (k: string, v: string) => data.set(k, v),
      getData: (k: string) => data.get(k) ?? "",
    };
    const leaf = screen.getByText("refunds.yaml");
    fireEvent.dragStart(leaf, { dataTransfer });
    // The scrollable list area is the root drop zone.
    const zone = leaf.closest(".scrollbar-thin")!;
    fireEvent.dragOver(zone, { dataTransfer });
    fireEvent.drop(zone, { dataTransfer });
    expect(onRename).toHaveBeenCalledWith("caps/refunds.yaml", "refunds.yaml");
  });
});
