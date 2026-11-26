import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { JSX, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import viteConfig from "../../vite.config.ts?raw";
import { useActive } from "./active";
import { ApiError, authRedirect } from "./api";
import {
  errorMessage,
  resetAssistant,
  SseParser,
  useAssistant,
} from "./assistant";
import { useLogout } from "./auth";
import { sse } from "@/test/sse";

const EDITOR = { open_file: "policy.yaml", unsaved: [] };

const TURN = sse(
  { type: "RUN_STARTED", threadId: "s1", runId: "t1" },
  { type: "TEXT_MESSAGE_START", messageId: "m1" },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "Lowered " },
  { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "the cap." },
  { type: "TEXT_MESSAGE_END", messageId: "m1" },
  { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "apply_change" },
  { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: '{"files":["a.y' },
  { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: 'aml"]}' },
  { type: "TOOL_CALL_END", toolCallId: "c1" },
  { type: "TOOL_CALL_RESULT", toolCallId: "c1", content: "applied" },
  {
    type: "CUSTOM",
    name: "change",
    value: { change_id: "chg_1", summary: "cap", files: [], checks: [] },
  },
  // Any other custom event, such as the end-of-turn usage, is not a change.
  { type: "CUSTOM", name: "usage", value: { input_tokens: 10 } },
  { type: "RUN_FINISHED", threadId: "s1", runId: "t1" },
);

/** A streamed response, and `release` to send its chunks and close it. */
function deferredStream() {
  const encoder = new TextEncoder();
  let release: (...chunks: string[]) => void = () => {};
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      release = (...chunks) => {
        for (const c of chunks) controller.enqueue(encoder.encode(c));
        controller.close();
      };
    },
  });
  const response = new Response(body, {
    headers: { "Content-Type": "text/event-stream" },
  });
  return { response, release };
}

function streamResponse(...chunks: string[]): Response {
  const { response, release } = deferredStream();
  release(...chunks);
  return response;
}

/** Stubs fetch: the n-th token request answers `token(n)`, a turn `turn()`. */
function stubFetch(
  opts: {
    token?: (n: number) => Response | undefined | Promise<Response | undefined>;
    turn?: () => Response;
  } = {},
) {
  let minted = 0;
  return vi.spyOn(window, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.endsWith("/assistant/token")) {
      minted += 1;
      return (
        (await opts.token?.(minted)) ??
        Response.json({
          token: `tok${minted}`,
          expires_at: "2026-11-23T12:00:00Z",
          can_write: true,
        })
      );
    }
    if (url.endsWith("/turns")) return opts.turn?.() ?? streamResponse(TURN);
    if (url.endsWith("/auth/cookie/logout")) return new Response(null);
    throw new Error(`unexpected fetch ${url}`);
  });
}

function calls(fetch: ReturnType<typeof stubFetch>, suffix: string) {
  return fetch.mock.calls.filter(([u]) => String(u).endsWith(suffix));
}

function wrapper({ children }: { children: ReactNode }): JSX.Element {
  const qc = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function renderAssistant() {
  return renderHook((props: { projectId: string }) => useAssistant(props), {
    initialProps: { projectId: "p1" },
    wrapper,
  });
}

beforeEach(() => {
  useActive.setState({ activeOrgId: "o1", activeProjectId: "p1" });
});

afterEach(() => {
  resetAssistant();
  vi.restoreAllMocks();
});

describe("vite dev proxy", () => {
  it("routes /v1/assistant to the assistant before /v1 takes it", () => {
    // The first matching key wins: listed after '/v1', turns reach the api.
    const assistant = viteConfig.search(/["']\/v1\/assistant["']\s*:/);
    expect(assistant).toBeGreaterThan(-1);
    expect(assistant).toBeLessThan(viteConfig.search(/["']\/v1["']\s*:/));
    expect(viteConfig.slice(assistant)).toMatch(/^[^}]*localhost:8090/);
  });
});

describe("SseParser", () => {
  it("returns the same events whatever the chunk boundaries", () => {
    const whole = new SseParser().feed(TURN);
    expect(whole).toHaveLength(13);

    const parser = new SseParser();
    const split = [...TURN].flatMap((char) => parser.feed(char));
    expect(split).toEqual(whole);
  });

  it("handles a split CRLF, comments, multi-line and non-JSON data", () => {
    const parser = new SseParser();
    const events = [
      ...parser.feed(': ping\r\n\r\ndata: {oops\n\ndata: {"type":\r'),
      ...parser.feed('\ndata: "RUN_FINISHED"}\r'),
      ...parser.feed("\n\r\n"),
    ];
    expect(events).toEqual([{ type: "RUN_FINISHED" }]);
  });
});

describe("errorMessage", () => {
  it.each([
    ["assistant_disabled", /not enabled/],
    ["user_not_verified", /Verify your email/],
    ["assistant_off", /turned off/],
    ["assistant_mode_unknown", /not recognised/],
  ])("maps the token refusal %s", (code, message) => {
    const err = new ApiError(403, { detail: { code } }, "403");
    expect(errorMessage(err)).toMatch(message);
  });
});

describe("useAssistant", () => {
  it("streams turns into the store, minting a token per message", async () => {
    const fetch = stubFetch();
    const { result } = renderAssistant();
    await act(() => result.current.send("lower the cap", EDITOR));
    await act(() => result.current.send("again", EDITOR));

    const turns = calls(fetch, "/turns");
    expect(String(turns[0][0])).toBe("/v1/assistant/projects/p1/turns");
    const inits = turns.map(([, init]) => init as RequestInit);
    expect(inits.map((i) => i.headers)).toMatchObject([
      { Authorization: "Bearer tok1" },
      { Authorization: "Bearer tok2" },
    ]);
    expect(inits.map((i) => JSON.parse(i.body as string))).toEqual([
      { session_id: null, message: "lower the cap", editor: EDITOR },
      { session_id: "s1", message: "again", editor: EDITOR },
    ]);

    const { state } = result.current;
    expect(state.sessionId).toBe("s1");
    expect(result.current.streaming).toBe(false);
    expect(state.messages[1].content).toBe("Lowered the cap.");
    const turn = state.messages[1].turn;
    expect(turn?.tools[0]).toMatchObject({
      name: "apply_change",
      args: { files: ["a.yaml"] },
      state: "completed",
      outputSummary: "applied",
    });
    expect(turn?.change?.change_id).toBe("chg_1");
    expect(turn?.error).toBeUndefined();
  });

  it("re-mints once on a 401 from the assistant and never redirects", async () => {
    const redirect = vi
      .spyOn(authRedirect, "toSignIn")
      .mockImplementation(() => {});
    const fetch = stubFetch({
      turn: () => new Response(null, { status: 401 }),
    });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));

    expect(calls(fetch, "/assistant/token")).toHaveLength(2);
    expect(redirect).not.toHaveBeenCalled();
    expect(result.current.state.messages[1].turn?.error).toMatch(
      /send it again/,
    );
    expect(result.current.streaming).toBe(false);
  });

  it("starts a paragraph for each text message of a turn", async () => {
    const text = (id: string, delta: string) => [
      { type: "TEXT_MESSAGE_START", messageId: id },
      { type: "TEXT_MESSAGE_CONTENT", messageId: id, delta },
      { type: "TEXT_MESSAGE_END", messageId: id },
    ];
    stubFetch({
      turn: () =>
        streamResponse(
          sse(
            ...text("m1", "Let me read the file."),
            { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "r" },
            { type: "TOOL_CALL_RESULT", toolCallId: "c1", content: "ok" },
            ...text("m2", "Done."),
            { type: "RUN_FINISHED" },
          ),
        ),
    });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));
    expect(result.current.state.messages[1].content).toBe(
      "Let me read the file.\n\nDone.",
    );
  });

  it("redirects on a 401 from the token mint and never retries it", async () => {
    const redirect = vi
      .spyOn(authRedirect, "toSignIn")
      .mockImplementation(() => {});
    const fetch = stubFetch({
      token: () => new Response(null, { status: 401 }),
    });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));

    // The dashboard session itself has expired: the usual sign-in redirect.
    expect(redirect).toHaveBeenCalledOnce();
    expect(calls(fetch, "/assistant/token")).toHaveLength(1);
    expect(calls(fetch, "/turns")).toHaveLength(0);
    expect(result.current.streaming).toBe(false);
  });

  it("never retries a turn whose stream has opened", async () => {
    const fetch = stubFetch({
      turn: () =>
        streamResponse(
          sse(
            { type: "RUN_STARTED", threadId: "s1", runId: "t1" },
            { type: "RUN_ERROR", message: "failed", code: "provider" },
          ),
        ),
    });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));

    expect(calls(fetch, "/assistant/token")).toHaveLength(1);
    expect(calls(fetch, "/turns")).toHaveLength(1);
    expect(result.current.streaming).toBe(false);
  });

  it.each([
    [403, /cannot work on this project/],
    [404, /no longer available/],
    [409, /still being answered/],
    [502, /could not reach the platform/],
    [503, /no model/],
  ])("shows the assistant's %i refusal", async (status, message) => {
    stubFetch({ turn: () => new Response(null, { status }) });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));
    expect(result.current.state.messages[1].turn?.error).toMatch(message);
  });

  it("shows a token refusal on the turn", async () => {
    stubFetch({
      token: () =>
        Response.json({ detail: { code: "assistant_off" } }, { status: 403 }),
    });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));
    expect(result.current.state.messages[1].turn?.error).toMatch(/turned off/);
  });

  it("ends a turn with a run error and fails its running tool", async () => {
    stubFetch({
      turn: () =>
        streamResponse(
          sse(
            { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "x" },
            {
              type: "RUN_ERROR",
              message: "This conversation is full; start a new chat.",
              code: "context_full",
            },
          ),
        ),
    });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));
    const turn = result.current.state.messages[1].turn;
    expect(turn?.error).toBe("This conversation is full; start a new chat.");
    expect(turn?.tools[0].state).toBe("failed");
  });

  it.each([
    ["limit_reached", "This answer reached its length limit."],
    ["limit_reached", "This answer reached its step limit."],
    ["provider", "The model answered in a way it cannot use."],
  ])("keeps the server's message for the run error %s", async (code, msg) => {
    stubFetch({
      turn: () =>
        streamResponse(sse({ type: "RUN_ERROR", message: msg, code })),
    });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));
    expect(result.current.state.messages[1].turn?.error).toBe(msg);
  });

  it("ends a turn whose stream closes without finishing", async () => {
    stubFetch({ turn: () => streamResponse(sse({ type: "RUN_STARTED" })) });
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));
    expect(result.current.streaming).toBe(false);
    expect(result.current.state.messages[1].turn?.error).toMatch(/closed/);
  });

  it("ignores a message sent while a turn is streaming", async () => {
    const fetch = stubFetch();
    const { result } = renderAssistant();
    await act(async () => {
      await Promise.all([
        result.current.send("one", EDITOR),
        result.current.send("two", EDITOR),
      ]);
    });
    expect(calls(fetch, "/turns")).toHaveLength(1);
    expect(result.current.state.messages).toMatchObject([
      { content: "one" },
      { role: "assistant" },
    ]);
  });

  it("aborts a running stream on logout so it cannot refill the store", async () => {
    const stream = deferredStream();
    const fetch = stubFetch({ turn: () => stream.response });
    const { result } = renderAssistant();
    let sent: Promise<void> = Promise.resolve();
    act(() => {
      sent = result.current.send("hi", EDITOR);
    });
    await waitFor(() => expect(calls(fetch, "/turns")).toHaveLength(1));

    const logout = renderHook(() => useLogout(), { wrapper });
    await act(() => logout.result.current.mutateAsync());
    const init = calls(fetch, "/turns")[0][1] as RequestInit;
    expect(init.signal?.aborted).toBe(true);

    await act(async () => {
      stream.release(TURN);
      await sent;
    });
    expect(result.current.state.messages).toEqual([]);
  });

  it("is empty after logout", async () => {
    stubFetch();
    const { result } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));
    expect(result.current.state.messages).toHaveLength(2);

    const logout = renderHook(() => useLogout(), { wrapper });
    await act(() => logout.result.current.mutateAsync());

    expect(result.current.state).toEqual({ sessionId: null, messages: [] });
    expect(result.current.streaming).toBe(false);
  });

  it("empties on a project switch and on an org switch", async () => {
    stubFetch();
    const { result, rerender } = renderAssistant();
    await act(() => result.current.send("hi", EDITOR));

    rerender({ projectId: "p2" });
    await waitFor(() => expect(result.current.state.messages).toEqual([]));

    await act(() => result.current.send("hi", EDITOR));
    expect(result.current.state.messages).toHaveLength(2);
    act(() => useActive.setState({ activeOrgId: "o2" }));
    await waitFor(() => expect(result.current.state.messages).toEqual([]));
  });

  it("drops the events of a turn whose project was switched away", async () => {
    const stream = deferredStream();
    const fetch = stubFetch({ turn: () => stream.response });
    const { result, rerender } = renderAssistant();
    let sent: Promise<void> = Promise.resolve();
    act(() => {
      sent = result.current.send("hi", EDITOR);
    });
    await waitFor(() => expect(calls(fetch, "/turns")).toHaveLength(1));

    rerender({ projectId: "p2" });
    // The old turn's request is cancelled, not just ignored.
    const init = calls(fetch, "/turns")[0][1] as RequestInit;
    await waitFor(() => expect(init.signal?.aborted).toBe(true));
    await act(async () => {
      stream.release(TURN);
      await sent;
    });
    expect(result.current.state.messages).toEqual([]);
    expect(result.current.state.sessionId).toBeNull();
  });

  it("keeps an old project's refusal off the new project's turn", async () => {
    let refuse: () => void = () => {};
    stubFetch({
      // The first project's token is refused only once we let it.
      token: async (n) => {
        if (n > 1) return undefined;
        await new Promise<void>((resolve) => (refuse = resolve));
        return Response.json(
          { detail: { code: "assistant_off" } },
          {
            status: 403,
          },
        );
      },
      // The second project's turn stays open.
      turn: () => deferredStream().response,
    });
    const { result, rerender } = renderAssistant();
    let first: Promise<void> = Promise.resolve();
    act(() => {
      first = result.current.send("on p1", EDITOR);
    });

    rerender({ projectId: "p2" });
    await waitFor(() => expect(result.current.state.messages).toEqual([]));
    act(() => {
      void result.current.send("on p2", EDITOR);
    });
    await act(async () => {
      refuse();
      await first;
    });

    expect(result.current.state.messages[1].turn?.error).toBeUndefined();
    expect(result.current.streaming).toBe(true);
  });
});
