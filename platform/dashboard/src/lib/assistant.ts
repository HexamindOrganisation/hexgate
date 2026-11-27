/**
 * The policy assistant's client: its config, one token per message, the turn
 * stream, and the chat store.
 *
 * The chat lives in a module-level store, not in component state: a stream
 * must survive the pane unmounting, as when the user leaves the Policies page
 * and comes back. The store belongs to one org and project;
 * moving to another, or signing out, empties it.
 *
 * A turn goes to the assistant service, not the api, with a token the api
 * mints for that one message. It goes through the shared `send()` with
 * `redirectOn401` off: a 401 from the assistant is an expired assistant token,
 * which says nothing about the dashboard session. The token mint keeps the
 * redirect, since a 401 there means the session itself has expired.
 */

import { useCallback, useEffect, useReducer } from "react";
import { useQuery } from "@tanstack/react-query";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import type {
  ChatMessageData,
  ChatToolCall,
  ChatTurn,
} from "@/components/chat/types";
import { useActive } from "./active";
import {
  ApiError,
  assistantApi,
  send,
  UnauthenticatedError,
  type ChangeCheck,
} from "./api";
import { randomId } from "./utils";

export type { AssistantConfig } from "./api";

// ——— Stream events (AG-UI names and payload fields) ———

/** A file and check summary of a saved change; the card fetches the rest. */
export interface AssistantChange {
  change_id: string;
  summary: string;
  files: { name: string; before_hash: string | null; after_hash: string }[];
  checks: ChangeCheck[];
}

export type AssistantEvent =
  | { type: "RUN_STARTED"; threadId: string }
  | { type: "TEXT_MESSAGE_START" }
  | { type: "TEXT_MESSAGE_CONTENT"; delta: string }
  | { type: "TOOL_CALL_START"; toolCallId: string; toolCallName: string }
  | { type: "TOOL_CALL_ARGS"; toolCallId: string; delta: string }
  | { type: "TOOL_CALL_END"; toolCallId: string }
  | { type: "TOOL_CALL_RESULT"; toolCallId: string; content: string }
  | { type: "CUSTOM"; name: string; value: unknown }
  | { type: "RUN_FINISHED" }
  | { type: "RUN_ERROR"; message: string; code?: string | null };

/**
 * Incremental parser for a `text/event-stream` body. A network chunk may end
 * anywhere, in the middle of an event or of a `\r\n`, so it buffers until a
 * blank line closes an event. Comment lines (the `: ping` keep-alive) and
 * events whose data is not JSON are dropped.
 */
export class SseParser {
  private buffer = "";
  private pendingCr = false;

  feed(chunk: string): unknown[] {
    let text = (this.pendingCr ? "\r" : "") + chunk;
    // A trailing `\r` may be the first half of a `\r\n` in the next chunk.
    this.pendingCr = text.endsWith("\r");
    if (this.pendingCr) text = text.slice(0, -1);
    this.buffer += text.replace(/\r\n?/g, "\n");

    const events: unknown[] = [];
    let end = this.buffer.indexOf("\n\n");
    while (end >= 0) {
      const event = parseEventBlock(this.buffer.slice(0, end));
      if (event !== undefined) events.push(event);
      this.buffer = this.buffer.slice(end + 2);
      end = this.buffer.indexOf("\n\n");
    }
    return events;
  }
}

function parseEventBlock(block: string): unknown {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""));
  if (!data.length) return undefined;
  try {
    return JSON.parse(data.join("\n"));
  } catch {
    return undefined;
  }
}

// ——— UI model ———

export interface AssistantToolCall extends ChatToolCall {
  /** The arguments as streamed, parsed into `args` when the call ends. */
  argsText: string;
}

export interface PolicyAssistantTurn extends ChatTurn {
  tools: AssistantToolCall[];
  /** The change this turn saved, when it saved one. */
  change?: AssistantChange;
}

export interface AssistantMessage extends ChatMessageData {
  turn?: PolicyAssistantTurn;
}

export interface AssistantState {
  /** The assistant's session; null until the first turn starts one. */
  sessionId: string | null;
  messages: AssistantMessage[];
}

/** What the policy editor holds when a message is sent. */
export interface AssistantEditor {
  open_file: string | null;
  unsaved: string[];
}

const INITIAL_STATE: AssistantState = {
  sessionId: null,
  messages: [],
};

/** Whether the chat's last turn is still being answered. */
export function isStreaming(state: AssistantState): boolean {
  return state.messages[state.messages.length - 1]?.turn?.streaming ?? false;
}

// ——— Pure reducer ———

/** The turn being streamed is always the last message. */
function updateLast(
  state: AssistantState,
  update: (
    message: AssistantMessage,
    turn: PolicyAssistantTurn,
  ) => AssistantMessage,
): AssistantState {
  const last = state.messages[state.messages.length - 1];
  if (!last?.turn) return state;
  return {
    ...state,
    messages: [...state.messages.slice(0, -1), update(last, last.turn)],
  };
}

function updateTurn(
  state: AssistantState,
  update: (turn: PolicyAssistantTurn) => PolicyAssistantTurn,
): AssistantState {
  return updateLast(state, (m, turn) => ({ ...m, turn: update(turn) }));
}

function updateTool(
  state: AssistantState,
  id: string,
  update: (tool: AssistantToolCall) => AssistantToolCall,
): AssistantState {
  return updateTurn(state, (turn) => ({
    ...turn,
    tools: turn.tools.map((t) => (t.id === id ? update(t) : t)),
  }));
}

function parseArgs(text: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(text);
    return value && typeof value === "object"
      ? (value as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

/** Ends the turn: a tool still running has failed with it. */
function endTurn(state: AssistantState, error?: string): AssistantState {
  return updateTurn(state, (turn) => ({
    ...turn,
    streaming: false,
    error,
    tools: turn.tools.map((t) =>
      t.state === "started"
        ? { ...t, state: "failed", endedAt: Date.now() }
        : t,
    ),
  }));
}

export function applyEvent(
  state: AssistantState,
  event: AssistantEvent,
): AssistantState {
  switch (event.type) {
    case "RUN_STARTED":
      return { ...state, sessionId: event.threadId };
    case "TEXT_MESSAGE_START":
      // A turn's text comes as one message per model step (before and after
      // each tool call); each starts its own paragraph.
      return updateLast(state, (m) =>
        m.content ? { ...m, content: m.content + "\n\n" } : m,
      );
    case "TEXT_MESSAGE_CONTENT":
      return updateLast(state, (m) => ({
        ...m,
        content: m.content + event.delta,
      }));
    case "TOOL_CALL_START":
      return updateTurn(state, (t) => ({
        ...t,
        tools: [
          ...t.tools,
          {
            id: event.toolCallId,
            name: event.toolCallName,
            args: {},
            argsText: "",
            state: "started",
            startedAt: Date.now(),
          },
        ],
      }));
    case "TOOL_CALL_ARGS":
      return updateTool(state, event.toolCallId, (c) => ({
        ...c,
        argsText: c.argsText + event.delta,
      }));
    case "TOOL_CALL_END":
      return updateTool(state, event.toolCallId, (c) => ({
        ...c,
        args: parseArgs(c.argsText),
      }));
    case "TOOL_CALL_RESULT":
      return updateTool(state, event.toolCallId, (c) => ({
        ...c,
        state: "completed",
        outputSummary: event.content,
        endedAt: Date.now(),
      }));
    case "CUSTOM":
      if (event.name !== "change") return state;
      return updateTurn(state, (t) => ({
        ...t,
        change: event.value as AssistantChange,
      }));
    case "RUN_FINISHED":
      return endTurn(state);
    case "RUN_ERROR":
      // The server words every run error for the user, and its code alone
      // does not say which (a step or a length limit, for one).
      return endTurn(state, event.message);
    default:
      return state;
  }
}

// ——— Refusals and errors, in the user's words ———

/** The ways a message can be refused before its turn runs: the token route's
 * codes and the assistant's refusals (plain HTTP, keyed by status). */
const MESSAGES: Record<string, string> = {
  assistant_disabled: "The assistant is not enabled on this platform.",
  user_not_verified: "Verify your email address to use the assistant.",
  assistant_off: "The assistant is turned off for this organization.",
  assistant_mode_unknown:
    "This organization's assistant setting is not recognised; an owner can set it again in the organization settings.",
  "401": "The assistant could not verify this message; send it again.",
  "403": "The assistant cannot work on this project.",
  "404": "This conversation is no longer available; start a new chat.",
  "409":
    "Another of your messages is still being answered; wait for it to finish.",
  "422":
    "The message is over 8 000 characters or malformed; shorten it and resend.",
  "502": "The assistant could not reach the platform; try again.",
  "503": "The assistant has no model configured.",
};

/** The most unsaved file names a turn may carry. */
const MAX_UNSAVED = 50;

function messageFor(code: unknown, fallback: string): string {
  return (typeof code === "string" && MESSAGES[code]) || fallback;
}

/** The assistant refused a turn before its stream opened. */
class TurnRefused extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`turn refused (${status})`);
    this.status = status;
  }
}

/** The message for an error that ended a turn: the assistant's refusal, read
 * from its HTTP status; a token route refusal, read from its
 * `{detail: {code}}` body; or a network failure. */
export function errorMessage(err: unknown): string {
  if (err instanceof TurnRefused) {
    return messageFor(
      String(err.status),
      `The assistant refused the message (${err.status}).`,
    );
  }
  if (!(err instanceof ApiError)) return "The assistant could not be reached.";
  const code = (err.detail as { detail?: { code?: unknown } } | null)?.detail
    ?.code;
  return messageFor(code, err.message);
}

// ——— Module-level store ———

const useStore = create<AssistantState>(() => INITIAL_STATE);
/** The org and project the store holds, or null before any. */
let scope: { orgId: string | null; projectId: string } | null = null;
/** The turn in flight. Emptying the store aborts it and clears this, and a
 * turn writes to the store only while it is still this one. */
let activeTurn: AbortController | null = null;

function clearStore(): void {
  activeTurn?.abort();
  activeTurn = null;
  useStore.setState(INITIAL_STATE, true);
}

/** Applies `update` unless the store has moved on from `turn`. */
function updateFromTurn(
  turn: AbortController,
  update: (s: AssistantState) => AssistantState,
): void {
  if (turn === activeTurn) useStore.setState(update, true);
}

/** Empties the chat and forgets its project. Called on logout: there is no
 * page reload then, so the next user would otherwise inherit the chat. */
export function resetAssistant(): void {
  scope = null;
  clearStore();
}

function ensureScope(orgId: string | null, projectId: string): void {
  if (scope?.orgId === orgId && scope.projectId === projectId) return;
  clearStore();
  scope = { orgId, projectId };
}

// ——— Transport ———

/** Mints this message's token and posts the turn. The token lives only in
 * this call. A refusal from the token route passes through as it is; one from
 * the assistant becomes a `TurnRefused`. */
async function postTurn(
  projectId: string,
  body: string,
  signal: AbortSignal,
): Promise<Response> {
  const { token } = await assistantApi.token(projectId);
  try {
    return await send(
      `/v1/assistant/projects/${projectId}/turns`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body,
        signal,
      },
      false,
    );
  } catch (err) {
    if (err instanceof UnauthenticatedError) throw new TurnRefused(401);
    if (err instanceof ApiError) throw new TurnRefused(err.status);
    throw err;
  }
}

async function readStream(
  body: ReadableStream<Uint8Array>,
  turn: AbortController,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    const events = parser.feed(decoder.decode(value, { stream: true }));
    // One store update per chunk, not per event.
    updateFromTurn(turn, (s) =>
      (events as AssistantEvent[]).reduce(applyEvent, s),
    );
  }
}

/** Posts the turn and returns its open stream; throws when the turn is
 * refused before any stream opens. */
async function openTurn(
  projectId: string,
  body: string,
  turn: AbortController,
): Promise<ReadableStream<Uint8Array>> {
  let res: Response;
  try {
    res = await postTurn(projectId, body, turn.signal);
  } catch (err) {
    // An expired token, refused before any stream opened: mint another, once.
    // A turn whose stream has opened is never retried; it ends with its events.
    if (!(err instanceof TurnRefused && err.status === 401)) throw err;
    res = await postTurn(projectId, body, turn.signal);
  }
  if (!res.body) throw new TurnRefused(res.status);
  return res.body;
}

async function readTurn(
  stream: ReadableStream<Uint8Array>,
  turn: AbortController,
): Promise<void> {
  await readStream(stream, turn);
  updateFromTurn(turn, (s) =>
    isStreaming(s)
      ? endTurn(s, "The connection closed before the answer finished.")
      : s,
  );
}

/** Sends a message on the store's project. Ignored while a turn runs.
 * Resolves once the turn ends: false when the message was refused or
 * ignored, so the caller can give its text back; true when its stream had
 * opened or the turn was cancelled. */
async function sendMessage(
  message: string,
  editor: AssistantEditor,
): Promise<boolean> {
  const state = useStore.getState();
  if (!scope || isStreaming(state)) return false;
  const projectId = scope.projectId;
  const turn = new AbortController();
  activeTurn = turn;
  const body = JSON.stringify({
    session_id: state.sessionId,
    message,
    editor: { ...editor, unsaved: editor.unsaved.slice(0, MAX_UNSAVED) },
  });
  useStore.setState({
    messages: [
      ...state.messages,
      { id: randomId(), role: "user", content: message },
      {
        id: randomId(),
        role: "assistant",
        content: "",
        turn: { streaming: true, reasoning: "", tools: [] },
      },
    ],
  });
  const fail = (err: unknown) =>
    updateFromTurn(turn, (s) => endTurn(s, errorMessage(err)));
  try {
    let stream: ReadableStream<Uint8Array>;
    try {
      stream = await openTurn(projectId, body, turn);
    } catch (err) {
      // Cancelled (New chat, another project, logout): nothing to give back.
      if (turn.signal.aborted) return true;
      fail(err);
      return false;
    }
    await readTurn(stream, turn).catch(fail);
    return true;
  } finally {
    if (turn === activeTurn) activeTurn = null;
  }
}

// ——— React ———

/** The Assistant tab's config for a project. Disabled while it is null. */
export function useAssistantConfig(projectId: string | null) {
  return useQuery({
    queryKey: ["assistant-config", projectId],
    queryFn: () => assistantApi.config(projectId as string),
    enabled: !!projectId,
    staleTime: 30_000,
  });
}

/** The changes the chat's turns saved, oldest first. */
export function changesOf(messages: AssistantMessage[]): AssistantChange[] {
  return messages.flatMap((m) => (m.turn?.change ? [m.turn.change] : []));
}

/** The latest of `changes` that wrote `name`, with the hash it left. */
export function lastChangeTo(
  changes: AssistantChange[],
  name: string,
): { change: AssistantChange; afterHash: string } | null {
  for (let i = changes.length - 1; i >= 0; i--) {
    const file = changes[i].files.find((f) => f.name === name);
    if (file) return { change: changes[i], afterHash: file.after_hash };
  }
  return null;
}

/** The changes the chat has saved on `projectId`, oldest first. None while
 * the store holds no chat, or another project's. Compared item by item, so a
 * streamed token does not re-render the caller. */
export function useAssistantChanges(
  projectId: string | null,
): AssistantChange[] {
  return useStore(
    useShallow((s) =>
      projectId !== null && scope?.projectId === projectId
        ? changesOf(s.messages)
        : [],
    ),
  );
}

/** The change bars closed on each project, as "change_id:file". Module-level,
 * so a bar stays closed after its file's tab closes or the page unmounts. */
const closedBars = new Map<string, Set<string>>();
const NONE_CLOSED: ReadonlySet<string> = new Set();

/** The change bars closed on `projectId`, and `close` to close one. */
export function useClosedChangeBars(projectId: string | null): {
  closed: ReadonlySet<string>;
  close: (key: string) => void;
} {
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  const close = useCallback(
    (key: string) => {
      if (projectId === null) return;
      const set = closedBars.get(projectId) ?? new Set<string>();
      closedBars.set(projectId, set.add(key));
      rerender();
    },
    [projectId],
  );
  const closed =
    (projectId !== null && closedBars.get(projectId)) || NONE_CLOSED;
  return { closed, close };
}

/** The chat for the active org's project. Another org or project empties it. */
export function useAssistant({ projectId }: { projectId: string }) {
  const orgId = useActive((s) => s.activeOrgId);
  const state = useStore();
  useEffect(() => {
    ensureScope(orgId, projectId);
  }, [orgId, projectId]);
  return {
    state,
    streaming: isStreaming(state),
    send: sendMessage,
    newChat: clearStore,
  };
}
