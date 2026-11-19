import type { ChatToolCall } from "./types";

/** What a call's card reports. A policy that stops a call doesn't always
 * fail it (the OpenAI and Google ADK adapters return the verdict as the
 * tool's result), so `hexgate serve` marks it on the event; an older serve
 * that doesn't is read from the message's opening instead. */
export type Verdict =
  | "allowed"
  | "denied"
  | "held"
  | "failed"
  | "running"
  | "awaiting";

// "[<marker>] <body>", as the SDK's verdict reader matches it (tool, reach
// or skill); the wording says held from denied.
const STOPPED_TEXT =
  /^\[[a-z0-9_-]+\] (?:Tool|reach to agent|skill) '[^']*' (requires human approval|is denied by the agent policy|is not permitted by this agent's policy)/;

export function verdictOf(call: ChatToolCall, awaiting: boolean): Verdict {
  if (call.state === "started") return awaiting ? "awaiting" : "running";
  if (call.verdict) return call.verdict;
  // null: serve said the tool ran. Only an older serve, with no field, is
  // read from the text.
  if (call.verdict === null)
    return call.state === "failed" ? "failed" : "allowed";
  const phrase = STOPPED_TEXT.exec(call.outputSummary?.trim() ?? "")?.[1];
  if (phrase) return phrase === "requires human approval" ? "held" : "denied";
  return call.state === "failed" ? "failed" : "allowed";
}

/** The key a held call and its approval prompt share: tool and arguments. */
export const callKey = (name: string, args: unknown) =>
  `${name}\u0000${JSON.stringify(args)}`;

/** Link props that open a new tab in full screen: a demo tab mustn't
 * navigate away. */
export function newTabWhen(present: boolean) {
  return present ? { target: "_blank", rel: "noopener" } : {};
}
