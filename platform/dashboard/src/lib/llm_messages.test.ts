/**
 * The two rules the drawer cannot get wrong: which turn is the anchor, and
 * which rows count as a gap. Both are matched on order alone — a wrong
 * anchor shows an auditor the exchange of some other tool call, and a false
 * gap accuses the pipeline of losing an event it never lost.
 */

import { describe, expect, it } from "vitest";

import type { LlmMessageRow } from "./api";
import { anchorTranscript } from "./llm_messages";

const DECISION_AT = "2026-06-01T10:00:00Z";

function row(
  eventId: string,
  occurredAt: string,
  messageSeq: number,
  turnKey = "run-a",
): LlmMessageRow {
  return {
    event_id: eventId,
    occurred_at: occurredAt,
    received_at: occurredAt,
    agent_name: "a",
    agent_version_id: "v1",
    session_id: "s1",
    user_id: "u1",
    model: "gpt-5",
    turn_key: turnKey,
    message_seq: messageSeq,
    resynced: false,
    truncated: false,
    input_messages: [],
    output_messages: [],
    system_instructions: null,
    run_id: null,
  };
}

const ids = (turns: { row: LlmMessageRow }[]) =>
  turns.map((t) => t.row.event_id);

describe("anchorTranscript()", () => {
  it("happy path: splits the rows around the decision", () => {
    const t = anchorTranscript(
      [
        row("m1", "2026-06-01T09:58:00Z", 0),
        row("m2", "2026-06-01T09:59:00Z", 1),
        row("m3", "2026-06-01T10:00:01Z", 2),
        row("m4", "2026-06-01T10:00:02Z", 3),
      ],
      DECISION_AT,
    );

    expect(ids(t.earlier)).toEqual(["m1"]);
    expect(t.anchor?.row.event_id).toBe("m2");
    expect(t.next?.row.event_id).toBe("m3");
    expect(ids(t.later)).toEqual(["m4"]);
  });

  it("when a turn shares the decision's timestamp then it is the anchor", () => {
    // The decision is caused by that call's completion, so the two can land
    // on the same instant at the pipeline's resolution. Reading it as "after"
    // would anchor on the previous call instead.
    const t = anchorTranscript(
      [row("m1", "2026-06-01T09:59:00Z", 0), row("m2", DECISION_AT, 1)],
      DECISION_AT,
    );

    expect(t.anchor?.row.event_id).toBe("m2");
    expect(t.next).toBeNull();
  });

  it("when no turn precedes the decision then there is no anchor", () => {
    // pydantic-ai emits one event per run, at the end — so every decision in
    // that run has the whole transcript after it and nothing to highlight.
    const t = anchorTranscript(
      [row("m1", "2026-06-01T10:00:05Z", 0)],
      DECISION_AT,
    );

    expect(t.anchor).toBeNull();
    expect(t.earlier).toEqual([]);
    expect(t.next?.row.event_id).toBe("m1");
  });

  it("when the run ends on the decision then nothing follows", () => {
    const t = anchorTranscript(
      [row("m1", "2026-06-01T09:59:00Z", 0)],
      DECISION_AT,
    );

    expect(t.anchor?.row.event_id).toBe("m1");
    expect(t.next).toBeNull();
  });

  it("when message_seq skips ahead then the row is a gap", () => {
    const t = anchorTranscript(
      [
        row("m1", "2026-06-01T09:58:00Z", 0),
        row("m2", "2026-06-01T09:59:00Z", 3),
      ],
      DECISION_AT,
    );

    expect(t.earlier[0].gap).toBe(false);
    expect(t.anchor?.gap).toBe(true);
  });

  it("when a list starts above zero then its first row is a gap", () => {
    const t = anchorTranscript(
      [row("m1", "2026-06-01T09:58:00Z", 4)],
      DECISION_AT,
    );

    expect(t.anchor?.gap).toBe(true);
  });

  it("when two lists interleave then neither is read as a gap in the other", () => {
    // A sub-agent keeps its own list and restarts message_seq at 0. Counting
    // across lists would call every one of its turns a lost event.
    const t = anchorTranscript(
      [
        row("m1", "2026-06-01T09:58:00Z", 0, "run-a"),
        row("m2", "2026-06-01T09:58:30Z", 0, "sub-b"),
        row("m3", "2026-06-01T09:59:00Z", 1, "run-a"),
        row("m4", "2026-06-01T09:59:30Z", 1, "sub-b"),
      ],
      DECISION_AT,
    );

    expect([...t.earlier, t.anchor!].map((turn) => turn.gap)).toEqual([
      false,
      false,
      false,
      false,
    ]);
  });

  it("when a retried insert duplicates a row then it is not a gap", () => {
    // Reads run without FINAL, so both copies of an event_id are visible
    // until the background merge collapses them. A repeated seq is not a
    // hole in the record.
    const t = anchorTranscript(
      [
        row("m1", "2026-06-01T09:58:00Z", 0),
        row("m1", "2026-06-01T09:58:00Z", 0),
      ],
      DECISION_AT,
    );

    expect(t.anchor?.gap).toBe(false);
  });
});
