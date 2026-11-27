/**
 * InspectorTabs tests — the Resolved table renders a role's tools + modes,
 * the Lints tab lists lints, and the empty states read correctly.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { QueryClient } from "@tanstack/react-query";

import type { PolicyLint, PolicyMatrix, ResolvedPolicy } from "@/lib/api";
import { InspectorTabs } from "./InspectorTabs";
import { resetAssistant } from "@/lib/assistant";
import { Route, Routes } from "react-router-dom";
import { makeQueryClient, renderWithProviders } from "@/test/render";
import { sseResponse } from "@/test/sse";

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
const EDITOR = { open_file: "policy.yaml", unsaved: [] };

/** The picker lists the project's agents from the manifest endpoint. */
function stubAgents(names: string[]) {
  vi.spyOn(window, "fetch").mockImplementation(
    async () =>
      new Response(JSON.stringify(names.map((name) => ({ name }))), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

function renderInspector(
  props: Partial<Parameters<typeof InspectorTabs>[0]> = {},
  client?: QueryClient,
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
      editor={EDITOR}
      {...props}
    />,
    { client },
  );
}

afterEach(() => vi.restoreAllMocks());

describe("InspectorTabs", () => {
  it("opens the hub in a new tab while edits are unsaved", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    stubAgents(["support_bot"]);
    renderInspector({ editor: { open_file: null, unsaved: ["a.yaml"] } });
    await userEvent.click(screen.getByRole("button", { name: /Graph/ }));
    expect(screen.getByText(/unsaved edits/)).toBeInTheDocument();
    await userEvent.click(
      await screen.findByRole("button", { name: "support_bot" }),
    );
    expect(open).toHaveBeenCalledWith(
      "/agents/support_bot",
      "_blank",
      "noopener",
    );
  });

  it("opens an agent's hub from the Graph picker", async () => {
    stubAgents(["billing_bot", "support_bot"]);
    renderWithProviders(
      <Routes>
        <Route
          path="/policies"
          element={
            <InspectorTabs
              projectId="p1"
              resolved={RESOLVED}
              matrix={MATRIX}
              lints={[]}
              draft={null}
              resolves
              modular
              previewing={false}
              agentNames={["billing_bot", "support_bot"]}
              inspectAgent="*"
              onInspectAgentChange={noop}
              editor={EDITOR}
            />
          }
        />
        <Route path="/agents/:name" element={<p>hub</p>} />
      </Routes>,
      { initialRoute: "/policies" },
    );
    await userEvent.click(screen.getByRole("button", { name: /Graph/ }));
    await userEvent.click(
      await screen.findByRole("button", { name: "support_bot" }),
    );
    expect(await screen.findByText("hub")).toBeInTheDocument();
  });

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
      screen.getByText(/Any tool not shown: support deny/),
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

  it("says the table is each tool's own entry, the grid the engine's", async () => {
    renderInspector();
    await userEvent.click(screen.getByRole("button", { name: "table" }));
    expect(
      screen.getByText(/the grid shows what the engine enforces/),
    ).toBeInTheDocument();
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

describe("InspectorTabs Assistant tab", () => {
  /** The config answers `enabled`; a turn streams one answer. */
  function stubAssistant(enabled: boolean) {
    return vi.spyOn(window, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/assistant/config")) {
        return Response.json({
          enabled,
          mode: "read_write",
          can_write: true,
          verified: true,
        });
      }
      if (url.endsWith("/assistant/token")) {
        return Response.json({ token: "t", expires_at: "x", can_write: true });
      }
      if (url.endsWith("/turns")) {
        return sseResponse(
          { type: "RUN_STARTED", threadId: "s1" },
          { type: "TEXT_MESSAGE_START" },
          { type: "TEXT_MESSAGE_CONTENT", delta: "The cap is 1000." },
          { type: "RUN_FINISHED" },
        );
      }
      return Response.json([]);
    });
  }

  afterEach(() => resetAssistant());

  it("is hidden when the platform disables the assistant", async () => {
    stubAssistant(false);
    const client = makeQueryClient();
    renderInspector({}, client);
    // The config has arrived, so the tab's absence is its answer.
    await waitFor(() =>
      expect(client.getQueryState(["assistant-config", "p1"])?.status).toBe(
        "success",
      ),
    );
    expect(
      screen.queryByRole("button", { name: "Assistant" }),
    ).not.toBeInTheDocument();
  });

  it("comes first, and its chat survives a switch to Lints", async () => {
    stubAssistant(true);
    renderInspector();
    const tab = await screen.findByRole("button", { name: "Assistant" });
    const tabs = screen.getAllByRole("button");
    expect(tabs.indexOf(tab)).toBeLessThan(
      tabs.indexOf(screen.getByRole("button", { name: "Resolved" })),
    );

    await userEvent.click(tab);
    await userEvent.type(
      screen.getByRole("textbox"),
      "what is the cap?{Enter}",
    );
    expect(await screen.findByText("The cap is 1000.")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Lints/ }));
    expect(screen.getByText("The cap is 1000.")).not.toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Assistant" }));
    expect(screen.getByText("what is the cap?")).toBeInTheDocument();
    expect(screen.getByText("The cap is 1000.")).toBeInTheDocument();
  });

  it("starts another project's chat with an empty composer", async () => {
    stubAssistant(true);
    const client = makeQueryClient();
    // Both projects' config is cached, so the pane stays mounted.
    for (const pid of ["p1", "p2"]) {
      client.setQueryData(["assistant-config", pid], {
        enabled: true,
        mode: "read_write",
        can_write: true,
        verified: true,
      });
    }
    const { rerender } = renderInspector({}, client);
    await userEvent.click(screen.getByRole("button", { name: "Assistant" }));
    await userEvent.type(screen.getByRole("textbox"), "for p1 only");
    rerender(
      <InspectorTabs
        projectId="p2"
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
        editor={EDITOR}
      />,
    );
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("keeps an unsent draft across a switch to Lints", async () => {
    stubAssistant(true);
    renderInspector();
    await userEvent.click(
      await screen.findByRole("button", { name: "Assistant" }),
    );
    await userEvent.type(screen.getByRole("textbox"), "half a question");
    await userEvent.click(screen.getByRole("button", { name: /Lints/ }));
    expect(screen.getByText(/No lints/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Assistant" }));
    expect(screen.getByRole("textbox")).toHaveValue("half a question");
  });
});
