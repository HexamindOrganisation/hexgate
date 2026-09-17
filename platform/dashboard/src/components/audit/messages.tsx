/**
 * The Audit drawer's "LLM messages" section: the exchange around one
 * decision, not a trace explorer.
 *
 * A transcript is anchored on the selected decision and matched by time
 * order within the session, never by content — the anchor turn is the last
 * message event at or before the decision (the prompt that produced the tool
 * call), and the turn after it is what the framework fed back plus what the
 * model said next. Everything else collapses.
 *
 * Content arrives already decoded from its stored JSON, in the OTel GenAI
 * shape: a message is `{role, parts}` and a part names itself under `type`.
 * A part this renderer does not know is printed whole rather than dropped,
 * which is how reasoning shows up: the OpenAI Agents adapter carries it
 * through on the input side and drops it from the completion, and issue #221
 * has not settled what the other adapters do. Printing the unknown part is
 * what lets this section stay correct either way — nothing below looks for a
 * thinking part, and nothing below assumes there is not one.
 */

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import type { LlmMessage, LlmMessagePart, LlmMessageRow } from "@/lib/api";
import { anchorTranscript, type Turn } from "@/lib/llm_messages";
import { fmtTs } from "./fmt";

/** Whatever a content field holds, as text — the fallback for a value that
 * is neither a message list nor empty.
 *
 * An empty array is empty, not content: a delta whose input list did not
 * grow still emits an event (the completion is new either way), and an
 * output whose every item was reasoning serialises to `[]` too. Rendering
 * the literal `[]` would put an empty bordered box under "Input" on those. */
function rawText(value: unknown): string | null {
  if (value == null || value === "") return null;
  if (Array.isArray(value))
    return value.length ? JSON.stringify(value, null, 2) : null;
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function Marker({ label, tone }: { label: string; tone: string }) {
  return (
    <span className={`rounded px-1.5 py-[1px] text-[10px] font-medium ${tone}`}>
      {label}
    </span>
  );
}

function Pre({ children }: { children: React.ReactNode }) {
  return (
    <pre className="m-0 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-foreground">
      {children}
    </pre>
  );
}

/** What a part IS, above the part itself.
 *
 * The role header alone cannot carry this: a completion that mixes a
 * sentence and a tool call puts both under one "assistant", and a reader
 * would have nothing to tell the model's answer from the call the policy
 * judged. Deliberately in the neutral label colour — `allow`/`deny`/
 * `approval` are outcome tokens, spent in this same card on the decision
 * badge and the marker chips, and a tool call carries no verdict.
 *
 * `id` is the call id: the only thing pairing a call with its return, and
 * the only way to tell which call a decision is about when several
 * parallel calls share one turn.
 */
function PartLabel({ label, id }: { label: string; id?: unknown }) {
  return (
    <div className="mb-0.5 flex items-baseline gap-1.5 text-[10px] uppercase tracking-wider text-muted-foreground">
      <span>{label}</span>
      {typeof id === "string" && id && (
        <span className="font-mono normal-case tracking-normal opacity-70">
          {id}
        </span>
      )}
    </div>
  );
}

function Part({ part }: { part: LlmMessagePart }) {
  if (part == null || typeof part !== "object")
    return <Pre>{String(part)}</Pre>;
  const type = part.type;
  if (type === "text") {
    const content = part.content;
    return (
      <Pre>
        {typeof content === "string"
          ? content
          : JSON.stringify(content, null, 2)}
      </Pre>
    );
  }
  if (type === "tool_call") {
    // `arguments` arrives as a string, not a parsed object: the adapter
    // keeps whatever the model emitted, and redaction only re-serialises it
    // when it parses (TOOL_CALL_JSON_KEYS). Malformed JSON therefore reaches
    // here verbatim, so print it rather than trying to parse it again.
    return (
      <div className="py-0.5">
        <PartLabel label="Tool call" id={part.id} />
        <Pre>
          {/* The accent, not an outcome colour: allow/deny/approval are
              verdicts, and this card already spends them on the decision
              badge and the marker chips. The tool name is the token you
              scan for and match against the decision row above, so it is
              the one thing here that earns a hue. */}
          <span className="font-medium text-primary">
            {String(part.name ?? "")}
          </span>
          {"("}
          {typeof part.arguments === "string"
            ? part.arguments
            : JSON.stringify(part.arguments)}
          {")"}
        </Pre>
      </div>
    );
  }
  if (type === "tool_call_response") {
    // The only place a tool's return value is stored at all: a decision row
    // records that the tool was called, never what came back.
    const response = part.response;
    return (
      <div className="py-0.5">
        <PartLabel label="Tool result" id={part.id} />
        <Pre>
          {typeof response === "string"
            ? response
            : JSON.stringify(response, null, 2)}
        </Pre>
      </div>
    );
  }
  // An item with no GenAI part to map onto — a reasoning item on the input
  // side, a built-in tool call, an image. Printed whole: the transcript
  // exists to explain a run after the fact. Labelled by its own `type` when
  // it declares one, so it is not mistaken for the model's prose.
  return (
    <div className="py-0.5">
      {typeof type === "string" && type && <PartLabel label={type} />}
      <Pre>{JSON.stringify(part, null, 2)}</Pre>
    </div>
  );
}

/** One message: who produced it, and the parts it holds.
 *
 * The role header is dropped for a tool message whose parts all name
 * themselves as results. "TOOL" then only repeats "TOOL RESULT" — the
 * speaker IS the tool, and the part identifies it better, by call id. The
 * assistant case is not symmetric and keeps both: a completion can mix a
 * sentence and a tool call under one role, so there the header says
 * something the part does not.
 */
function Message({ message }: { message: LlmMessage }) {
  const parts = Array.isArray(message?.parts) ? message.parts : [];
  const roleIsRedundant =
    message?.role === "tool" &&
    parts.length > 0 &&
    parts.every((part) => part?.type === "tool_call_response");
  return (
    <div className="border-t border-border/60 px-2.5 py-1.5 first:border-t-0">
      {!roleIsRedundant && (
        <div className="mb-1 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
          {String(message?.role ?? "—")}
        </div>
      )}
      {parts.length ? (
        parts.map((part, i) => <Part key={i} part={part} />)
      ) : (
        <Pre>{JSON.stringify(message, null, 2)}</Pre>
      )}
    </div>
  );
}

/** One content column.
 *
 * `bare` is for `system_instructions`, which is a list of *parts* rather
 * than of messages — the adapters build it as `[text_part(prompt)]`, and
 * `gen_ai.system_instructions` has no role to carry. Rendering it through
 * `Message` would find no `parts` key and print the system prompt as raw
 * JSON under an empty role. */
function Field({
  label,
  value,
  bare,
}: {
  label: string;
  value: unknown;
  bare?: boolean;
}) {
  const items = Array.isArray(value) && value.length ? value : null;
  const raw = items ? null : rawText(value);
  if (!items && !raw) return null;
  return (
    <div className="mt-2">
      <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="overflow-hidden rounded-md border border-border bg-muted">
        {items ? (
          bare ? (
            <div className="px-2.5 py-1.5">
              {(items as LlmMessagePart[]).map((part, i) => (
                <Part key={i} part={part} />
              ))}
            </div>
          ) : (
            (items as LlmMessage[]).map((message, i) => (
              <Message key={i} message={message} />
            ))
          )
        ) : (
          <div className="px-2.5 py-1.5">
            <Pre>{raw}</Pre>
          </div>
        )}
      </div>
    </div>
  );
}

function TurnCard({
  turn,
  highlight,
  caption,
}: {
  turn: Turn;
  highlight?: boolean;
  caption?: string;
}) {
  const { row } = turn;
  return (
    <div
      data-testid="llm-turn"
      className={`rounded-lg border p-2.5 ${
        highlight ? "border-primary/45 bg-primary/5" : "border-border"
      }`}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
        {caption && (
          <span className="font-medium text-foreground">{caption}</span>
        )}
        <span className="font-mono">{fmtTs(new Date(row.occurred_at))}</span>
        <span className="font-mono">{row.model}</span>
        <span className="font-mono">#{row.message_seq}</span>
        {turn.gap && (
          <Marker label="transcript incomplete" tone="bg-deny/15 text-deny" />
        )}
        {row.resynced && (
          <Marker
            label="history restated"
            tone="bg-approval/15 text-approval"
          />
        )}
        {row.truncated && (
          <Marker label="truncated" tone="bg-approval/15 text-approval" />
        )}
      </div>
      <Field label="System instructions" value={row.system_instructions} bare />
      <Field label="Input" value={row.input_messages} />
      <Field label="Output" value={row.output_messages} />
    </div>
  );
}

/** A collapsed run of turns — the conversation before the anchor, and
 * anything after the turn that followed it. */
function CollapsedTurns({ turns, label }: { turns: Turn[]; label: string }) {
  const [open, setOpen] = useState(false);
  if (!turns.length) return null;
  const Icon = open ? ChevronDown : ChevronRight;
  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-[11.5px] text-muted-foreground hover:bg-accent"
      >
        <Icon className="size-3.5" />
        {turns.length} {label}
        {turns.some((t) => t.gap) && (
          <Marker label="transcript incomplete" tone="bg-deny/15 text-deny" />
        )}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {turns.map((turn, i) => (
            <TurnCard key={`${turn.row.event_id}:${i}`} turn={turn} />
          ))}
        </div>
      )}
    </div>
  );
}

export function LlmMessagesSection({
  rows,
  total,
  decisionOccurredAt,
  scoped,
  partial,
  isLoading,
  isError,
}: {
  rows: LlmMessageRow[];
  total: number;
  decisionOccurredAt: string;
  /** The decision carries a session id or a run id. Without either there is
   * no transcript to name, and the endpoint 422s by design. */
  scoped: boolean;
  /** Rows exist after this window, so its last turn has a successor we did
   * not fetch. The drawer's head-then-tail windowing normally lands on the
   * transcript's true end and this is false; it can still be true if rows
   * land between the two requests. */
  partial: boolean;
  isLoading: boolean;
  isError: boolean;
}) {
  const note = (text: string) => (
    <div className="text-xs text-muted-foreground">{text}</div>
  );
  if (!scoped)
    return note(
      "No session or run id on this decision — nothing to scope a transcript to.",
    );
  if (isLoading) return note("Loading…");
  if (isError) return note("Could not load the transcript.");
  if (!rows.length) return note("No LLM messages recorded for this session.");

  const { earlier, anchor, next, later } = anchorTranscript(
    rows,
    decisionOccurredAt,
  );
  // The anchor is only "the call that produced this decision" if we can see
  // that nothing sits between it and the decision. At the far edge of a
  // partial window we cannot, so the card says what it actually is — the
  // last turn we have — rather than asserting an identity the page cannot
  // establish.
  // No `next` means the anchor is the window's last row.
  const anchorAtEdge = partial && !next;

  return (
    <div>
      {total > rows.length && (
        <div className="mb-2 text-[11px] text-muted-foreground">
          Showing {rows.length} of {total} turns.
        </div>
      )}
      <CollapsedTurns turns={earlier} label="earlier turns" />
      <div className="space-y-2">
        {anchor ? (
          <TurnCard
            turn={anchor}
            highlight
            caption={anchorAtEdge ? "Last turn loaded" : "This call"}
          />
        ) : (
          note("No message event precedes this decision.")
        )}
        {next ? (
          <TurnCard turn={next} caption="What followed" />
        ) : (
          // Never "the run ended": the follow-up turn may simply not have
          // landed yet — decisions and messages reach ClickHouse by
          // different paths — or may sit outside this window.
          note("No later turn recorded.")
        )}
      </div>
      {later.length > 0 && (
        <div className="mt-2">
          <CollapsedTurns turns={later} label="later turns" />
        </div>
      )}
    </div>
  );
}
