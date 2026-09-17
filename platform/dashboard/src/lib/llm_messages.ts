/**
 * Anchoring a session transcript on one audit decision.
 *
 * The rules the Audit drawer renders, kept apart from the rendering: which
 * turn produced the selected decision, which followed it, and where the
 * pipeline lost an event in between. Pure functions over the rows the
 * llm-messages endpoint returns.
 */

import type { LlmMessage, LlmMessageRow } from "@/lib/api";

/** One row plus the markers that only its neighbours can establish. */
export interface Turn {
  row: LlmMessageRow;
  /** `message_seq` skipped ahead within this row's `turn_key`: the pipeline
   * lost an event, and the conversation shown here is missing a step. */
  gap: boolean;
}

export interface Transcript {
  earlier: Turn[];
  /** The turn that produced the selected decision; null when no message
   * event precedes it. That is the shape planned for the pydantic-ai
   * adapter, which has no per-call hook and so is specced to emit one event
   * per run, at the end — every decision in such a run has the whole
   * transcript after it. */
  anchor: Turn | null;
  /** The turn that followed the decision; null when the run ended on it. */
  next: Turn | null;
  later: Turn[];
}

/**
 * Mark `message_seq` gaps, then split the rows around the decision.
 *
 * Gaps are per `turn_key`, because one session holds several message lists
 * — the main run, each sub-agent, each handoff — and `message_seq` restarts
 * at 0 in each, so a jump only means loss within one list. The test is
 * `seq > previous + 1`, strictly greater: a batch insert retried after a
 * partial failure leaves two copies of an `event_id` visible until the
 * background merge collapses them, and `!== previous + 1` would read that
 * duplicate as a hole in the record.
 *
 * `rows` are expected oldest-first, as the endpoint returns them.
 */
export function anchorTranscript(
  rows: LlmMessageRow[],
  decisionOccurredAt: string,
): Transcript {
  const decidedAt = new Date(decisionOccurredAt).getTime();
  const lastSeq = new Map<string, number>();
  const turns: Turn[] = rows.map((row) => {
    const previous = lastSeq.get(row.turn_key);
    lastSeq.set(row.turn_key, row.message_seq);
    return {
      row,
      // A list's first seen row is a gap only if it does not start at 0;
      // the page starts at the transcript's head, so there is nothing
      // before it that paging could explain away.
      gap:
        previous === undefined
          ? row.message_seq > 0
          : row.message_seq > previous + 1,
    };
  });

  // The last turn at or before the decision. `<=` rather than `<`: the
  // decision is caused by the completion of that same call, and the two can
  // share a timestamp at the pipeline's resolution.
  let anchorIndex = -1;
  turns.forEach((turn, i) => {
    if (new Date(turn.row.occurred_at).getTime() <= decidedAt) anchorIndex = i;
  });

  return {
    earlier: turns.slice(0, Math.max(anchorIndex, 0)),
    anchor: anchorIndex >= 0 ? turns[anchorIndex] : null,
    next: turns[anchorIndex + 1] ?? null,
    later: turns.slice(anchorIndex + 2),
  };
}

/** Assign a colour slot to every tool call, in order of appearance.
 *
 * **A hue does not identify a call.** It groups one call with its own
 * result, and the same hue recurs elsewhere in the transcript — it can even
 * recur inside one card. A card shows the previous turn's results above this
 * turn's calls, so up to six ids are visible at once, and the palette caps
 * at three: three hues cannot tell six things apart, and no assignment rule
 * changes that. The id printed beside every dot is the identifier; the dot
 * is a shortcut for the eye, and following it to the wrong one costs a
 * glance, not a wrong conclusion.
 *
 * (An earlier version of this comment claimed a repeat "never lands adjacent
 * to its twin". False: three parallel calls followed by one more gives the
 * fourth the first's hue, and the first's result sits directly above it.)
 *
 * Three is the most that clears the colour-vision floors against both
 * surfaces, so widening the cycle is not available. Within one turn the
 * calls do stay distinct, which is the case the colour was added for; a turn
 * making more than three gets no dot past the third rather than an
 * immediately repeated hue.
 */
export function buildCallSlots(rows: LlmMessageRow[]): Map<string, number> {
  const slots = new Map<string, number>();
  let next = 0;
  for (const row of rows) {
    const messages = Array.isArray(row.output_messages)
      ? (row.output_messages as LlmMessage[])
      : [];
    const ids = messages
      .flatMap((message) =>
        Array.isArray(message?.parts) ? message.parts : [],
      )
      .filter((part) => part?.type === "tool_call")
      .map((part) => part.id)
      .filter((id): id is string => typeof id === "string" && !!id);
    ids.slice(0, 3).forEach((id, index) => slots.set(id, (next + index) % 3));
    // Advance by the slots actually handed out, not by the call count: a
    // turn of four would otherwise leave `next` where a three-call turn does
    // and hand the next turn a hue still on screen.
    next = (next + Math.min(ids.length, 3)) % 3;
  }
  return slots;
}
