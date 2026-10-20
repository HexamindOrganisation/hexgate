/**
 * InspectorTabs tests — the Resolved table renders a role's tools + modes,
 * the Lints tab lists lints, and the empty states read correctly.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PolicyLint, PolicyMatrix, ResolvedPolicy } from "@/lib/api";
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

const MATRIX: PolicyMatrix = {
  roles: ["support"],
  tools: ["read_ticket", "refund"],
  cells: {
    read_ticket: { support: { mode: "allow", constraint: null } },
    refund: {
      support: { mode: "approval", constraint: "args.amount <= 100" },
    },
  },
  defaults: { support: { mode: "deny", constraint: null } },
  aliased_default: null,
};

const noop = () => undefined;

function renderInspector(
  props: Partial<Parameters<typeof InspectorTabs>[0]> = {},
) {
  return renderWithProviders(
    <InspectorTabs
      projectId="p1"
      resolved={RESOLVED}
      matrix={MATRIX}
      lints={[]}
      draft={null}
      resolves
      modular
      previewing={false}
      agentNames={[]}
      inspectAgent="*"
      onInspectAgentChange={noop}
      {...props}
    />,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("InspectorTabs", () => {
  it("opens Resolved on a tools × roles grid", () => {
    renderInspector();
    expect(
      screen.getByRole("columnheader", { name: "support" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("rowheader", { name: "read_ticket" }),
    ).toBeInTheDocument();
    // Constrained cells say so to screen readers and show the rules on hover.
    const refund = screen.getByRole("button", {
      name: /approval, constrained/,
    });
    expect(refund).toHaveAttribute("title", "args.amount <= 100");
    // Tools the policy doesn't list fall back per role.
    expect(
      screen.getByText(/Tools not listed: support deny/),
    ).toBeInTheDocument();
  });

  it("states a fenced fallback with its condition", () => {
    renderInspector({
      matrix: {
        ...MATRIX,
        defaults: { support: { mode: "allow", constraint: "user.tier == 1" } },
      },
    });
    expect(
      screen.getByText(/support allow \(if user\.tier == 1\)/),
    ).toBeInTheDocument();
  });

  it("says when the grid couldn't load", () => {
    renderInspector({ matrix: null });
    expect(screen.getByText(/Couldn't load the grid/)).toBeInTheDocument();
  });

  it("drills from a grid cell into the role's table", async () => {
    renderInspector();
    await userEvent.click(
      screen.getByRole("button", { name: /approval, constrained/ }),
    );
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

  it("lists the entry's agents (plus the generic *) and selects one", async () => {
    const onChange = vi.fn();
    renderInspector({
      agentNames: ["billing_bot", "support_bot"],
      onInspectAgentChange: onChange,
    });
    const select = screen.getByRole("combobox");
    // "*" generic view plus every declared agent are offered as options.
    expect(
      screen.getByRole("option", { name: "* (generic)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "support_bot" }),
    ).toBeInTheDocument();
    await userEvent.selectOptions(select, "support_bot");
    expect(onChange).toHaveBeenCalledWith("support_bot");
  });

  it("relabels agent.* / mcp keys as admission / reach / mcp", () => {
    const resolved: ResolvedPolicy = {
      support: {
        default_policy: { mode: "deny" },
        tools: {
          "agent.run": { mode: "allow", constraints: [] },
          "agent.handoff:billing_bot": { mode: "allow", constraints: [] },
          "mcp-kb-search": { mode: "allow", constraints: [] },
          read_ticket: { mode: "allow", constraints: [] },
        },
      },
    };
    const tools = [
      "agent.run",
      "agent.handoff:billing_bot",
      "mcp-kb-search",
      "read_ticket",
    ];
    const allow = { support: { mode: "allow", constraint: null } } as const;
    const matrix: PolicyMatrix = {
      ...MATRIX,
      tools,
      cells: Object.fromEntries(tools.map((t) => [t, allow])),
    };
    renderInspector({ resolved, matrix });
    // agent.run → admission; agent.handoff:X → reach target + "handoff";
    // mcp-X → the name without the prefix. Raw agent.* keys never render.
    expect(screen.getByText("start this agent")).toBeInTheDocument();
    expect(screen.getByText("admission")).toBeInTheDocument();
    expect(screen.getByText("billing_bot")).toBeInTheDocument();
    expect(screen.getByText("handoff")).toBeInTheDocument();
    expect(screen.getByText("kb-search")).toBeInTheDocument();
    expect(screen.queryByText("agent.run")).not.toBeInTheDocument();
    expect(
      screen.queryByText("agent.handoff:billing_bot"),
    ).not.toBeInTheDocument();
  });
});
