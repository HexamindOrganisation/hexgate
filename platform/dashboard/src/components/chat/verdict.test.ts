import { describe, expect, it } from "vitest";
import type { ChatToolCall } from "./types";
import { callKey, newTabWhen, verdictOf } from "./verdict";

const call = (over: Partial<ChatToolCall>): ChatToolCall => ({
  id: "c1",
  name: "t",
  args: {},
  state: "completed",
  startedAt: 0,
  ...over,
});

describe("verdictOf", () => {
  it("test_verdictOf_happy_path", () => {
    expect(verdictOf(call({}), false)).toBe("allowed");
  });

  it("test_when_started_then_running_or_awaiting", () => {
    expect(verdictOf(call({ state: "started" }), false)).toBe("running");
    expect(verdictOf(call({ state: "started" }), true)).toBe("awaiting");
  });

  it("test_when_serve_marks_verdict_then_it_wins_over_the_text", () => {
    const text = "[tool] Tool 'x' is denied by the agent policy";
    expect(
      verdictOf(call({ verdict: "held", outputSummary: text }), false),
    ).toBe("held");
    expect(verdictOf(call({ verdict: null, outputSummary: text }), false)).toBe(
      "allowed",
    );
    expect(verdictOf(call({ verdict: null, state: "failed" }), false)).toBe(
      "failed",
    );
  });

  it("test_when_older_serve_then_verdict_is_read_from_the_text", () => {
    const held = "[tool] Tool 'x' requires human approval";
    const denied = "[tool] Tool 'x' is denied by the agent policy";
    expect(verdictOf(call({ outputSummary: held }), false)).toBe("held");
    expect(verdictOf(call({ outputSummary: denied }), false)).toBe("denied");
  });
});

describe("callKey", () => {
  it("test_callKey_happy_path", () => {
    expect(callKey("t", { a: 1 })).toBe(callKey("t", { a: 1 }));
    expect(callKey("t", { a: 1 })).not.toBe(callKey("t", { a: 2 }));
  });
});

describe("newTabWhen", () => {
  it("test_when_present_then_opens_a_new_tab", () => {
    expect(newTabWhen(true)).toEqual({ target: "_blank", rel: "noopener" });
    expect(newTabWhen(false)).toEqual({});
  });
});
