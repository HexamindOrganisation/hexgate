/**
 * AssistantPane tests: the model's text renders without links or remote
 * images, tool steps as plain text, and the composer holds a message while an
 * answer streams.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { resetAssistant } from "@/lib/assistant";
import { renderWithProviders } from "@/test/render";
import { sseResponse } from "@/test/sse";
import { AssistantPane } from "./AssistantPane";

const EDITOR = { open_file: "policy.yaml", unsaved: ["caps/a.yaml"] };

/** Each turn answers the next response from `turns`; null keeps the turn
 * streaming. */
function stubTurns(turns: (Response | null)[]) {
  return vi.spyOn(window, "fetch").mockImplementation(async (input) => {
    if (String(input).endsWith("/assistant/token")) {
      return Response.json({ token: "t", expires_at: "x", can_write: true });
    }
    return turns.shift() ?? new Response(new ReadableStream());
  });
}

function turnBodies(fetch: ReturnType<typeof stubTurns>) {
  return fetch.mock.calls
    .filter(([u]) => String(u).endsWith("/turns"))
    .map(([, init]) => JSON.parse((init as RequestInit).body as string));
}

function transcriptOf(container: HTMLElement): HTMLElement {
  return container.querySelector(".overflow-y-auto") as HTMLElement;
}

/** jsdom lays nothing out: give the transcript a scrollable height. */
function stubSize(el: HTMLElement) {
  Object.defineProperty(el, "scrollHeight", { value: 1000 });
  Object.defineProperty(el, "clientHeight", { value: 100 });
}

/** The transcript, scrolled up to its top by the user. */
function scrolledUp(container: HTMLElement): HTMLElement {
  const el = transcriptOf(container);
  stubSize(el);
  el.scrollTop = 0;
  fireEvent.scroll(el);
  return el;
}

async function ask(text: string) {
  await userEvent.type(screen.getByRole("textbox"), `${text}{Enter}`);
}

afterEach(() => {
  resetAssistant();
  vi.restoreAllMocks();
});

describe("AssistantPane", () => {
  it("renders no link and no remote image from the model's text", async () => {
    const text =
      "See ![x](https://evil.test/a.png) [the docs](https://evil.test/d) " +
      'and <img src="https://evil.test/b.png"> **done**';
    stubTurns([
      sseResponse(
        { type: "TEXT_MESSAGE_START" },
        { type: "TEXT_MESSAGE_CONTENT", delta: text },
        { type: "RUN_FINISHED" },
      ),
    ]);
    const { container } = renderWithProviders(
      <AssistantPane projectId="p1" editor={EDITOR} />,
    );
    await ask("hi");
    // Markdown still renders; a link keeps its text.
    expect(await screen.findByText("done")).toBeInTheDocument();
    expect(screen.getByText(/the docs/)).toBeInTheDocument();
    // Raw HTML shows as text; nothing in the page points anywhere.
    expect(
      container.querySelector("[data-streamdown=strong]"),
    ).toHaveTextContent("done");
    expect(container.querySelector("a, img")).toBeNull();
    expect(container.querySelector("[src], [href], [srcset]")).toBeNull();
  });

  it("shows a tool step's arguments and result as plain text", async () => {
    stubTurns([
      sseResponse(
        { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "read" },
        { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: '{"name":"a"}' },
        { type: "TOOL_CALL_END", toolCallId: "c1" },
        {
          type: "TOOL_CALL_RESULT",
          toolCallId: "c1",
          content: "**bold** ![i](https://evil.test/i.png)",
        },
        { type: "RUN_FINISHED" },
      ),
    ]);
    const { container } = renderWithProviders(
      <AssistantPane projectId="p1" editor={EDITOR} />,
    );
    await ask("read it");
    await waitFor(() =>
      expect(container.querySelector("summary")).toHaveTextContent(
        "read · done",
      ),
    );
    expect(screen.getByText('{"name":"a"}')).toBeInTheDocument();
    expect(
      screen.getByText("**bold** ![i](https://evil.test/i.png)"),
    ).toBeInTheDocument();
    expect(container.querySelector("img, strong")).toBeNull();
  });

  it("holds a message typed while an answer streams", async () => {
    const fetch = stubTurns([null]);
    renderWithProviders(<AssistantPane projectId="p1" editor={EDITOR} />);
    await ask("first");
    await waitFor(() => expect(turnBodies(fetch)).toHaveLength(1));
    await ask("second");

    expect(screen.getByRole("textbox")).toHaveValue("second");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(turnBodies(fetch)).toEqual([
      { session_id: null, message: "first", editor: EDITOR },
    ]);
  });

  it("pins the view to the bottom when a message is sent", async () => {
    stubTurns([sseResponse({ type: "RUN_FINISHED" })]);
    const { container } = renderWithProviders(
      <AssistantPane projectId="p1" editor={EDITOR} />,
    );
    const transcript = scrolledUp(container);
    await ask("hi");
    await waitFor(() => expect(transcript.scrollTop).toBe(1000));
  });

  it("neither shows nor scrolls while another tab is active", async () => {
    stubTurns([sseResponse({ type: "RUN_FINISHED" })]);
    const { container, rerender } = renderWithProviders(
      <AssistantPane projectId="p1" editor={EDITOR} active={false} />,
    );
    const box = screen.getByRole("textbox", { hidden: true });
    expect(box).not.toBeVisible();
    const transcript = transcriptOf(container);
    stubSize(transcript);
    // A turn streams in while the pane is hidden: it does not scroll.
    fireEvent.change(box, { target: { value: "hi" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByText("hi")).toBeInTheDocument();
    expect(transcript.scrollTop).toBe(0);
    // Back on the tab, it catches up with the bottom.
    rerender(<AssistantPane projectId="p1" editor={EDITOR} />);
    expect(screen.getByRole("textbox")).toBeVisible();
    expect(transcript.scrollTop).toBe(1000);
  });

  it("takes at most the route's 8000 characters", () => {
    renderWithProviders(<AssistantPane projectId="p1" editor={EDITOR} />);
    expect(screen.getByRole("textbox")).toHaveAttribute("maxLength", "8000");
  });

  it("starts a new chat", async () => {
    stubTurns([sseResponse({ type: "RUN_FINISHED" })]);
    renderWithProviders(<AssistantPane projectId="p1" editor={EDITOR} />);
    await ask("hello");
    expect(await screen.findByText("hello")).toBeInTheDocument();
    // The answer has finished, so the composer sends again.
    await userEvent.type(screen.getByRole("textbox"), "more");
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: /New chat/ }));
    expect(screen.queryByText("hello")).not.toBeInTheDocument();
  });
});
