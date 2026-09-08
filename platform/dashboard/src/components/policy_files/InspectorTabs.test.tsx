/**
 * InspectorTabs tests — the Resolved table renders a role's tools + modes,
 * the Lints tab lists lints, and the empty states read correctly.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PolicyLint, ResolvedPolicy } from "@/lib/api";
import { InspectorTabs } from "./InspectorTabs";
import { renderWithProviders } from "@/test/render";

const RESOLVED: ResolvedPolicy = {
  support: {
    default_policy: { mode: "deny" },
    tools: {
      read_ticket: { mode: "allow", constraints: [] },
      refund: {
        mode: "approval_required",
        constraints: ["args.amount <= 100"],
      },
    },
  },
};

const noop = () => undefined;

function renderInspector(
  props: Partial<Parameters<typeof InspectorTabs>[0]> = {},
) {
  return renderWithProviders(
    <InspectorTabs
      projectId="p1"
      resolved={RESOLVED}
      lints={[]}
      draft={null}
      resolves
      modular
      previewing={false}
      inspectAgent="*"
      onInspectAgentChange={noop}
      {...props}
    />,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("InspectorTabs", () => {
  it("renders a role's tools with mode badges in the Resolved tab", () => {
    renderInspector();
    expect(screen.getByText("read_ticket")).toBeInTheDocument();
    expect(screen.getByText("refund")).toBeInTheDocument();
    expect(screen.getByText("args.amount <= 100")).toBeInTheDocument();
  });

  it("shows the compose-doesn't-resolve empty state", () => {
    renderInspector({ resolves: false, resolved: null });
    expect(screen.getByText(/doesn't compose/i)).toBeInTheDocument();
  });

  it("lists lints in the Lints tab", async () => {
    const lints: PolicyLint[] = [
      {
        code: "dead-grant",
        severity: "warning",
        message: "grant never used",
        source: "caps.yaml",
        tier: null,
        tool: "refund",
        role: "support",
      },
    ];
    renderInspector({ lints });
    await userEvent.click(screen.getByRole("button", { name: /lints/i }));
    expect(screen.getByText("grant never used")).toBeInTheDocument();
  });

  it("hides the graph launcher on a classic project", () => {
    renderInspector({ modular: false });
    expect(
      screen.queryByRole("button", { name: /graph/i }),
    ).not.toBeInTheDocument();
  });

  it("offers the graph launcher when modular", () => {
    renderInspector();
    expect(screen.getByRole("button", { name: /graph/i })).toBeInTheDocument();
  });

  it("commits the agent field on Enter, not per keystroke", async () => {
    const onChange = vi.fn();
    renderInspector({ onInspectAgentChange: onChange });
    const input = screen.getByPlaceholderText("*");
    await userEvent.clear(input);
    await userEvent.type(input, "bot");
    expect(onChange).not.toHaveBeenCalled(); // no per-keystroke round-trips
    await userEvent.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith("bot");
  });
});
