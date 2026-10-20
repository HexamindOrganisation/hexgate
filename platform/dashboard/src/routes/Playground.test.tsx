/**
 * PlaygroundPage UI tests — the chat surface: composer sends, the agent turn
 * shows inline decision cards + the thinking loader, session settings set the
 * acting roles, and the accent stays inside the Playground. The live hook, project scope, resolved
 * policy, and agent fetch are all mocked so we exercise the view in isolation.
 */

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type {
  ApprovalRequestEvent,
  PlaygroundState,
  ToolCall,
} from "@/lib/playground";
import { renderWithProviders } from "@/test/render";
import { usePlaygroundPrefs } from "@/lib/playground_prefs";

const sendChat = vi.fn();
const reset = vi.fn();
const respondToApproval = vi.fn();
let mockState: PlaygroundState;

vi.mock("@/lib/active", () => ({
  useProjectScoped: () => ({ status: "ready", projectId: "p1" }),
}));
vi.mock("@/lib/playground", async (importActual) => ({
  // The real buildChatFrame drives the settings' context preview.
  ...(await importActual<typeof import("@/lib/playground")>()),
  usePlayground: () => ({
    state: mockState,
    sendChat,
    reset,
    respondToApproval,
  }),
}));
vi.mock("@/lib/policy_files", () => {
  // A stable reference — a fresh object each call would re-derive roleOptions
  // every render and spin the role-sync effect into an infinite loop.
  const resolved = { data: { support: {}, billing: {} } };
  return { useResolvedPolicy: () => resolved };
});
vi.mock("@/lib/api", () => ({
  api: {
    getAgent: vi.fn().mockResolvedValue({ name: "bot", policy_yaml: "" }),
  },
}));

// Imported after the mocks so the module picks them up.
import { PlaygroundPage } from "./Playground";

function baseState(over: Partial<PlaygroundState> = {}): PlaygroundState {
  return {
    connected: true,
    agentOnline: true,
    agentName: "billing-bot",
    messages: [],
    decisions: [],
    currentTurnId: null,
    pendingApprovals: [],
    ...over,
  };
}

function tool(over: Partial<ToolCall> = {}): ToolCall {
  return {
    id: "t1",
    name: "refund_order",
    args: {},
    state: "completed",
    startedAt: 0,
    endedAt: 12,
    ...over,
  };
}

function approval(
  over: Partial<ApprovalRequestEvent> = {},
): ApprovalRequestEvent {
  return {
    type: "approval.request",
    decision_id: "dec1",
    tool_name: "refund_order",
    arguments: {},
    reason: null,
    agent_name: "billing-bot",
    role: "billing",
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    ...over,
  };
}

// jsdom doesn't implement Element.scrollTo; the transcript auto-scroll effect
// calls it on every message change.
beforeAll(() => {
  Element.prototype.scrollTo = vi.fn();
});

afterEach(() => {
  vi.clearAllMocks();
  usePlaygroundPrefs.setState({ accent: "", names: {} });
});

function turnWith(tools: ToolCall[]): PlaygroundState["messages"] {
  return [
    { id: "u1", role: "user", content: "refund order A-1001" },
    {
      id: "a1",
      role: "assistant",
      content: "Done.",
      turn: { id: "a1", streaming: false, text: "Done.", reasoning: "", tools },
    },
  ];
}

describe("PlaygroundPage", () => {
  it("sends the composer text on Enter", async () => {
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "refund it{Enter}",
    );
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0][0]).toBe("refund it");
  });

  it("offers the acting-as control and the agent status", () => {
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    // The acting identity (name + role) sits in the composer.
    // The first role (policy order) is active to start.
    expect(screen.getByTitle(/acting as/i)).toHaveTextContent(
      "playground·billing",
    );
    expect(screen.getByText("billing-bot")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
  });

  it("renders tool calls inline, opening failures by default", () => {
    mockState = baseState({
      messages: turnWith([
        tool({ id: "t1", name: "lookup_order", args: { id: "A-1001" } }),
        tool({
          id: "t2",
          state: "failed",
          args: { amount: 400 },
          outputSummary: "denied: args.amount <= 50",
        }),
      ]),
    });
    renderWithProviders(<PlaygroundPage />);
    const [allowed, failed] = screen.getAllByRole("button", {
      expanded: undefined,
      name: /lookup_order|refund_order/,
    });
    expect(allowed).toHaveTextContent('Allowedlookup_order(id="A-1001")');
    expect(allowed).toHaveAttribute("aria-expanded", "false");
    expect(failed).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/denied: args.amount <= 50/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in audit" })).toHaveAttribute(
      "href",
      expect.stringContaining("tool=refund_order"),
    );
  });

  it("expands an allowed call on click", async () => {
    mockState = baseState({
      messages: turnWith([tool({ outputSummary: "refunded $40" })]),
    });
    renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: /refund_order/ }));
    expect(screen.getByText(/refunded \$40/)).toBeInTheDocument();
  });

  it("shows the thinking loader while a turn streams with no text yet", () => {
    mockState = baseState({
      messages: [
        {
          id: "a1",
          role: "assistant",
          content: "",
          turn: {
            id: "a1",
            streaming: true,
            text: "",
            reasoning: "",
            tools: [],
          },
        },
      ],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByRole("status")).toHaveTextContent(/thinking/i);
  });

  it("sums up the session's decisions in the top bar", () => {
    mockState = baseState({
      decisions: [tool({ id: "d1" }), tool({ id: "d2", state: "failed" })],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText(/2 decisions · 1 failed/)).toBeInTheDocument();
  });

  it("applies roles from session settings to the next message", async () => {
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    const dialog = screen.getByRole("dialog", { name: "Session settings" });
    await userEvent.type(screen.getByPlaceholderText("playground"), "maya");
    await userEvent.click(screen.getByRole("button", { name: "support" }));
    // The preview shows exactly what rides with the next turn.
    expect(dialog).toHaveTextContent('"user":"maya"');
    expect(dialog).toHaveTextContent('"roles":["billing","support"]');
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", {
      roles: ["billing", "support"],
      name: "maya",
    });
  });

  it("drops the draft on Cancel", async () => {
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "billing" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByTitle(/acting as/i)).toHaveTextContent("billing");
  });

  it("paints the accent on the Playground only, not the page", () => {
    usePlaygroundPrefs.setState({ accent: "rose" });
    mockState = baseState();
    const { container } = renderWithProviders(<PlaygroundPage />);
    const root = container.querySelector<HTMLElement>("[style*='--primary']");
    expect(root?.style.getPropertyValue("--action")).toMatch(/^340 /);
    expect(document.documentElement.style.getPropertyValue("--primary")).toBe(
      "",
    );
  });

  it("asks for approvals above the composer", () => {
    mockState = baseState({ pendingApprovals: [approval()] });
    renderWithProviders(<PlaygroundPage />);
    expect(
      screen.getByRole("button", { name: /approve/i }),
    ).toBeInTheDocument();
  });

  it("keeps a docs link reachable mid-session, not just on the empty state", () => {
    mockState = baseState({
      messages: [{ id: "u1", role: "user", content: "hi" }],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByRole("link", { name: "Docs" })).toBeInTheDocument();
  });

  it("keeps the offline notice visible mid-session", () => {
    mockState = baseState({
      agentOnline: false,
      messages: [{ id: "u1", role: "user", content: "hi" }],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText(/agent offline/i)).toBeInTheDocument();
  });

  it("shows the no-agent status when nothing is serving", () => {
    mockState = baseState({ agentName: null, agentOnline: false });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText(/no agent serving/i)).toBeInTheDocument();
  });

  it("still surfaces relay reconnecting when no agent is serving", () => {
    mockState = baseState({
      agentName: null,
      agentOnline: false,
      connected: false,
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument();
  });
});
