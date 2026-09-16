/**
 * Anchoring a session transcript on one audit decision.
 *
 * The rules the Audit drawer renders, kept apart from the rendering: which
 * turn produced the selected decision, which followed it, and where the
 * pipeline lost an event in between. Pure functions over the rows the
 * llm-messages endpoint returns.
 */

import type { LlmMessageRow } from "@/lib/api";

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
