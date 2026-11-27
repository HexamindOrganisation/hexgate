/**
 * ChangeCard tests: the checks from the stream, the changed files, and the
 * picked file's diff read from the stored change.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { AssistantChange } from "@/lib/assistant";
import type { ChangeRead } from "@/lib/api";
import { renderWithProviders } from "@/test/render";
import { ChangeCard } from "./ChangeCard";

// CodeMirror is slow to start under jsdom and FilesDiff has its own tests:
// show the two texts it is given instead.
vi.mock("./FilesDiff", () => ({
  FilesDiff: ({ before, after }: { before: string | null; after: string }) => (
    <div>
      <pre aria-label="before">{before ?? "(none)"}</pre>
      <pre aria-label="after">{after}</pre>
    </div>
  ),
}));

const CHANGE: AssistantChange = {
  change_id: "chg_1",
  summary: "Lower the billing refund cap to 500",
  files: [
    { name: "caps/billing.yaml", before_hash: "b1", after_hash: "a1" },
    { name: "caps/new.yaml", before_hash: null, after_hash: "a2" },
  ],
  checks: [
    { id: "yaml", status: "pass" },
    { id: "lints", status: "warn", detail: "unknown-tool on refund_order" },
  ],
};

const STORED: ChangeRead = {
  id: "chg_1",
  project_id: "p1",
  session_id: "s1",
  summary: CHANGE.summary,
  created_at: "2026-11-26T00:00:00Z",
  files: [
    {
      name: "caps/billing.yaml",
      before_content: "refund_cap: 1000\n",
      before_hash: "b1",
      after_content: "refund_cap: 500\n",
      after_hash: "a1",
    },
    {
      name: "caps/new.yaml",
      before_content: null,
      before_hash: null,
      after_content: "approval: true\n",
      after_hash: "a2",
    },
  ],
  checks: CHANGE.checks,
  effect: {
    changes: [
      {
        agent: "billing",
        role: "member",
        tool: "refund_order",
        before: { mode: "allow" },
        after: { mode: "approval_required" },
      },
    ],
  },
};

function stubChange(res: Response) {
  return vi.spyOn(window, "fetch").mockResolvedValue(res);
}

afterEach(() => vi.restoreAllMocks());

describe("ChangeCard", () => {
  it("lists each check with its status and detail", () => {
    stubChange(Response.json(STORED));
    renderWithProviders(<ChangeCard projectId="p1" change={CHANGE} />);
    expect(screen.getByText(CHANGE.summary)).toBeInTheDocument();
    const yaml = screen.getByText("Every file parses").closest("li")!;
    expect(yaml.querySelector("[aria-label=passed]")).not.toBeNull();
    const lints = screen.getByText("No new lint error").closest("li")!;
    expect(lints.querySelector("[aria-label=warning]")).not.toBeNull();
    expect(lints).toHaveTextContent("unknown-tool on refund_order");
  });

  it("shows a check id or status this build does not know", () => {
    stubChange(Response.json(STORED));
    const checks = [
      { id: "edits", status: "queued" },
    ] as unknown as AssistantChange["checks"];
    renderWithProviders(
      <ChangeCard projectId="p1" change={{ ...CHANGE, checks }} />,
    );
    const row = screen.getByText("edits").closest("li")!;
    // Shown as it came, never as a status this build knows.
    expect(row).toHaveTextContent("status: queued");
    expect(row.querySelector("[aria-label='status: queued']")).not.toBeNull();
    expect(row.querySelector("[aria-label=skipped]")).toBeNull();
    expect(row).not.toHaveTextContent("skipped");
  });

  it("reads the stored change and shows the first file's diff", async () => {
    const fetch = stubChange(Response.json(STORED));
    renderWithProviders(<ChangeCard projectId="p1" change={CHANGE} />);
    expect(await screen.findByLabelText("before")).toHaveTextContent(
      "refund_cap: 1000",
    );
    expect(screen.getByLabelText("after")).toHaveTextContent("refund_cap: 500");
    expect(String(fetch.mock.calls[0][0])).toContain(
      "/v1/projects/p1/policy/changes/chg_1",
    );
  });

  it("shows another file's diff when it is picked", async () => {
    stubChange(Response.json(STORED));
    renderWithProviders(<ChangeCard projectId="p1" change={CHANGE} />);
    const created = screen.getByRole("tab", { name: /caps\/new\.yaml/ });
    expect(created).toHaveTextContent("new");
    await userEvent.click(created);
    expect(created).toHaveAttribute("aria-selected", "true");
    await waitFor(() =>
      expect(screen.getByLabelText("after")).toHaveTextContent(
        "approval: true",
      ),
    );
    expect(screen.getByLabelText("before")).toHaveTextContent("(none)");
  });

  it("shows the effect from the same stored change on the Effect tab", async () => {
    const fetch = stubChange(Response.json(STORED));
    renderWithProviders(<ChangeCard projectId="p1" change={CHANGE} />);
    await userEvent.click(screen.getByRole("tab", { name: "Effect" }));
    expect(
      await screen.findByText(/billing · member · refund_order/),
    ).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("says so when the stored change cannot be read", async () => {
    stubChange(new Response("not found", { status: 404 }));
    renderWithProviders(<ChangeCard projectId="p1" change={CHANGE} />);
    expect(
      await screen.findByText("The change's files could not be loaded."),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Effect" }));
    expect(
      screen.getByText("The change's effect could not be loaded."),
    ).toBeInTheDocument();
  });
});
