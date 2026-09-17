/**
 * PlaygroundPage UI tests — the modern chat surface: composer sends, the agent
 * turn shows tool steps + the thinking loader, decisions float as a deck, and
 * the acting-as control lists roles. The live hook, project scope, resolved
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

const sendChat = vi.fn();
const reset = vi.fn();
const respondToApproval = vi.fn();
let mockState: PlaygroundState;

vi.mock("@/lib/active", () => ({
  useProjectScoped: () => ({ status: "ready", projectId: "p1" }),
}));
vi.mock("@/lib/playground", () => ({
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

afterEach(() => vi.clearAllMocks());

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
    expect(screen.getByText("acting as")).toBeInTheDocument();
    expect(screen.getByText("billing-bot")).toBeInTheDocument();
    expect(screen.getByText("connected")).toBeInTheDocument();
  });

  it("renders the agent turn's tool steps", () => {
    mockState = baseState({
      messages: [
        { id: "u1", role: "user", content: "refund order A-1001" },
        {
          id: "a1",
          role: "assistant",
          content: "Done.",
          turn: {
            id: "a1",
            streaming: false,
            text: "Done.",
            reasoning: "",
            tools: [tool()],
          },
        },
      ],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText("refund_order")).toBeInTheDocument();
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

  it("floats the decision deck with a running count", () => {
    mockState = baseState({
      decisions: [tool({ id: "d1" }), tool({ id: "d2", name: "send_email" })],
    });
    renderWithProviders(<PlaygroundPage />);
    // The deck's count badge and the pile cards render the decisions.
    expect(
      screen.getByRole("button", { name: /2 policy decisions/i }),
    ).toBeInTheDocument();
  });

  it("pins the decision deck open on click (touch/keyboard reachable)", async () => {
    mockState = baseState({ decisions: [tool({ id: "d1" })] });
    renderWithProviders(<PlaygroundPage />);
    const handle = screen.getByRole("button", { name: /1 policy decisions/i });
    expect(handle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(handle);
    expect(handle).toHaveAttribute("aria-expanded", "true");
  });

  it("hides the deck while an approval is pending so Approve stays clickable", () => {
    mockState = baseState({
      decisions: [tool({ id: "d1" })],
      pendingApprovals: [approval()],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(
      screen.queryByRole("button", { name: /policy decisions/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /approve/i }),
    ).toBeInTheDocument();
  });

  it("mutes the deck while the composer has text so it can't cover Send", async () => {
    mockState = baseState({ decisions: [tool({ id: "d1" })] });
    renderWithProviders(<PlaygroundPage />);
    // Visible while the composer is empty …
    expect(
      screen.getByRole("button", { name: /1 policy decisions/i }),
    ).toBeInTheDocument();
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "refund it",
    );
    // … muted (aria-hidden) once there's text to send, so a Send click can't
    // land on the deck instead.
    expect(
      screen.queryByRole("button", { name: /policy decisions/i }),
    ).not.toBeInTheDocument();
  });

  it("takes the muted deck handle out of the tab order (a11y)", () => {
    // aria-hidden hides it from getByRole, so query the DOM to prove the handle
    // is disabled — not a focusable control inside an aria-hidden subtree.
    mockState = baseState({
      decisions: [tool({ id: "d1" })],
      pendingApprovals: [approval()],
    });
    const { container } = renderWithProviders(<PlaygroundPage />);
    const handle = container.querySelector(
      'button[aria-label*="policy decisions"]',
    );
    expect(handle).toBeDisabled();
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
