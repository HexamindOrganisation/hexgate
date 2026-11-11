/**
 * PlaygroundPage UI tests — the chat surface: composer sends, the agent turn
 * shows inline decision cards + the thinking loader, session settings set the
 * acting roles, and the accent stays inside the Playground. The live hook, project scope, resolved
 * policy, and agent fetch are all mocked so we exercise the view in isolation.
 */

import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import { fireEvent, screen } from "@testing-library/react";
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

const scope = vi.hoisted(() => ({ projectId: "p1" }));
vi.mock("@/lib/active", () => ({
  useProjectScoped: () => ({ status: "ready", projectId: scope.projectId }),
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
// A stable reference — a fresh object each call would re-derive roleOptions
// every render and spin the role-sync effect into an infinite loop. Swap it
// (then rerender) to have the agent's roles come or go.
const ROLES = { data: { support: {}, billing: {} } };
const NO_ROLES = { data: {} };
const policy = vi.hoisted(() => ({ resolved: { data: {} } as object }));
vi.mock("@/lib/policy_files", () => ({
  useResolvedPolicy: () => policy.resolved,
}));
const getAgent = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", () => ({ api: { getAgent } }));

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

beforeEach(() => {
  getAgent.mockResolvedValue({ name: "bot", policy_yaml: "" });
  policy.resolved = ROLES;
  scope.projectId = "p1";
});

afterEach(() => {
  vi.clearAllMocks();
  usePlaygroundPrefs.setState({ accent: "", names: {}, roles: {} });
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

  it("doesn't count an Apply with nothing to pick as choosing no role", async () => {
    // Apply before `hexgate serve` is up: no roles to choose from yet.
    policy.resolved = NO_ROLES;
    mockState = baseState({ agentOnline: false });
    const { rerender } = renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    policy.resolved = ROLES;
    mockState = baseState();
    rerender(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", {
      roles: ["billing"],
      name: "",
    });
  });

  it("keeps the role choice while serve restarts", async () => {
    mockState = baseState();
    const { rerender } = renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "support" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    // The agent drops off (no roles), then comes back.
    policy.resolved = NO_ROLES;
    mockState = baseState({ agentOnline: false });
    rerender(<PlaygroundPage />);
    policy.resolved = ROLES;
    mockState = baseState();
    rerender(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", {
      roles: ["billing", "support"],
      name: "",
    });
  });

  it("keeps a no-role choice through an Apply while serve restarts", async () => {
    mockState = baseState();
    const { rerender } = renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "billing" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    policy.resolved = NO_ROLES;
    mockState = baseState({ agentOnline: false });
    rerender(<PlaygroundPage />);
    // Only the name changes; the roles list is empty because serve is down.
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    policy.resolved = ROLES;
    mockState = baseState();
    rerender(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", { roles: [], name: "" });
  });

  it("drops the roles when the serving agent defines none", async () => {
    mockState = baseState();
    const { rerender } = renderWithProviders(<PlaygroundPage />);
    expect(screen.getByTitle(/acting as/i)).toHaveTextContent("billing");
    policy.resolved = NO_ROLES;
    mockState = baseState({ agentName: "plain-bot" });
    rerender(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", { roles: [], name: "" });
  });

  it("keeps a no-role choice per project", async () => {
    mockState = baseState();
    const { rerender } = renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "billing" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    // Project B starts on its first role; back on A, "no role" still holds.
    scope.projectId = "p2";
    rerender(<PlaygroundPage />);
    expect(screen.getByTitle(/acting as/i)).toHaveTextContent("billing");
    scope.projectId = "p1";
    rerender(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", { roles: [], name: "" });
  });

  it("uses the compose roles even when the agent lookup fails", async () => {
    getAgent.mockRejectedValue(new Error("not registered"));
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", {
      roles: ["billing"],
      name: "",
    });
  });

  it("keeps the chosen roles on a turn sent while serve is down", async () => {
    mockState = baseState();
    const { rerender } = renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "support" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    policy.resolved = NO_ROLES;
    mockState = baseState({ agentOnline: false });
    rerender(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", {
      roles: ["billing", "support"],
      name: "",
    });
  });

  it("keeps the roles on a name-only Apply from a dialog opened offline", async () => {
    policy.resolved = NO_ROLES;
    mockState = baseState({ agentOnline: false });
    const { rerender } = renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    // Serve comes back while the dialog is open.
    policy.resolved = ROLES;
    mockState = baseState();
    rerender(<PlaygroundPage />);
    await userEvent.type(screen.getByPlaceholderText("playground"), "maya");
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", {
      roles: ["billing"],
      name: "maya",
    });
  });

  it("shows a denial returned as the tool's result as Denied", () => {
    // The OpenAI and Google ADK adapters return the verdict as a string.
    mockState = baseState({
      messages: turnWith([
        tool({
          state: "completed",
          outputSummary:
            "[policy_denied] Tool 'refund_order' is denied by the agent policy. The tool was not executed.",
        }),
      ]),
      decisions: [tool({ state: "completed" })],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText("Denied")).toBeInTheDocument();
    expect(screen.queryByText("Allowed")).toBeNull();
    // Open by default, so the reason is in view.
    expect(
      screen.getByText(/is denied by the agent policy/),
    ).toBeInTheDocument();
  });

  it("trusts serve's verdict, whatever the adapter's shape", () => {
    // LangChain: the denial streams as failed; serve marks it denied. And a
    // long denial whose summary was cut still reads as one.
    mockState = baseState({
      messages: turnWith([
        tool({
          id: "t1",
          state: "failed",
          verdict: "denied",
          outputSummary: "nope",
        }),
        tool({
          id: "t2",
          name: "issue_refund",
          state: "completed",
          verdict: "held",
          outputSummary: `[approval_required] ${"x".repeat(200)}...`,
        }),
      ]),
      decisions: [
        tool({ id: "t1", state: "failed", verdict: "denied" }),
        tool({ id: "t2", state: "completed", verdict: "held" }),
      ],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText("Denied")).toBeInTheDocument();
    expect(screen.getByText("Not approved")).toBeInTheDocument();
    expect(screen.queryByText("Failed")).toBeNull();
    expect(
      screen.getByText(/2 decisions · 1 denied · 1 not approved/),
    ).toBeInTheDocument();
  });

  it("doesn't carry a saved role choice to another agent", async () => {
    usePlaygroundPrefs.setState({
      roles: { p1: { agent: "other-bot", roles: [] } },
    });
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    // billing-bot is serving: its first role, not other-bot's "no role".
    expect(sendChat).toHaveBeenCalledWith("hi", {
      roles: ["billing"],
      name: "",
    });
  });

  it("says a call held at the approval prompt awaits approval", () => {
    mockState = baseState({
      // The running turn, with a call the prompt holds (same tool, same args).
      messages: turnWith([tool({ state: "started", endedAt: undefined })]).map(
        (m) => (m.turn ? { ...m, turn: { ...m.turn, streaming: true } } : m),
      ),
      pendingApprovals: [approval()],
    });
    renderWithProviders(<PlaygroundPage />);
    expect(screen.getByText("Awaiting approval")).toBeInTheDocument();
    expect(screen.queryByText("Running")).toBeNull();
  });

  it("keeps the applied roles after leaving the page and coming back", async () => {
    mockState = baseState();
    const { unmount } = renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "billing" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    // e.g. "Open in audit", then back.
    unmount();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.type(
      screen.getByLabelText("Message the agent"),
      "hi{Enter}",
    );
    expect(sendChat).toHaveBeenCalledWith("hi", { roles: [], name: "" });
  });

  it("caps a large argument in the call header", () => {
    mockState = baseState({
      messages: turnWith([tool({ args: { body: "x".repeat(500) } })]),
    });
    renderWithProviders(<PlaygroundPage />);
    const header = screen.getByText(/^\(body=/);
    expect(header.textContent!.length).toBeLessThan(60);
  });

  it("doesn't send on the Enter that confirms an IME candidate", () => {
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    const box = screen.getByLabelText("Message the agent");
    fireEvent.change(box, { target: { value: "返金" } });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    // Safari: the confirming Enter comes as keyCode 229, not isComposing.
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(sendChat).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(sendChat).toHaveBeenCalledTimes(1);
  });

  it("names the default caller when nothing identifies the turn", async () => {
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    await userEvent.click(screen.getByRole("button", { name: "billing" }));
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByTitle(/acting as/i)).toHaveTextContent(
      /^ddefault caller$/,
    );
  });

  it("says which policy decides for the roles picked", async () => {
    mockState = baseState();
    renderWithProviders(<PlaygroundPage />);
    await userEvent.click(screen.getByRole("button", { name: "Settings" }));
    const dialog = screen.getByRole("dialog", { name: "Session settings" });
    expect(dialog).toHaveTextContent("That role's policy decides");
    await userEvent.click(screen.getByRole("button", { name: "billing" }));
    expect(dialog).toHaveTextContent("No role selected");
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
    usePlaygroundPrefs.setState({ accent: "#ff0000" });
    mockState = baseState();
    const { container } = renderWithProviders(<PlaygroundPage />);
    const root = container.querySelector<HTMLElement>("[style*='--primary']");
    expect(root?.style.getPropertyValue("--action")).toBe("0 100% 50%");
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
