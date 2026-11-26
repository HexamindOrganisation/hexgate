import { memo, useEffect, useRef } from "react";
import { defaultRehypePlugins, Streamdown } from "streamdown";
import { Hexagon, RotateCcw } from "lucide-react";

import { Composer } from "@/components/chat/Composer";
import { ThinkingGoo } from "@/components/chat/ThinkingGoo";
import {
  useAssistant,
  type AssistantEditor,
  type AssistantMessage,
  type AssistantToolCall,
} from "@/lib/assistant";
import { cn } from "@/lib/utils";

/** The model's text quotes policy files and audit values, which anyone with
 * write access to them controls, so it must not make the browser fetch
 * anything or offer a link to click. Raw HTML is left out (it then shows as
 * text), and the two elements markdown itself can point elsewhere with are
 * dropped, a link's text kept. */
const REHYPE_PLUGINS = Object.entries(defaultRehypePlugins)
  .filter(([name]) => name !== "raw")
  .map(([, plugin]) => plugin);
const DISALLOWED = ["a", "img"];

/**
 * The Assistant tab: the chat with the policy assistant about this project,
 * its tool steps, and the composer. The chat lives in the module store
 * (lib/assistant.ts), so it survives this pane being unmounted.
 */
export function AssistantPane({
  projectId,
  editor,
  active = true,
}: {
  projectId: string;
  /** What the editor holds, sent with each message. */
  editor: AssistantEditor;
  /** False while another tab shows: the pane stays mounted, so the draft in
   * the composer survives, but it is hidden and does not scroll. */
  active?: boolean;
}) {
  const { state, streaming, send, newChat } = useAssistant({ projectId });
  const transcriptRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  // Follow the answer as it streams, unless the user has scrolled up to read;
  // on coming back to the tab, catch up with what streamed meanwhile.
  useEffect(() => {
    const el = transcriptRef.current;
    if (active && el && atBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [state.messages, active]);

  return (
    <div className="h-full flex flex-col" hidden={!active}>
      <div
        ref={transcriptRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          atBottomRef.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        }}
        className="flex-1 overflow-y-auto scrollbar-thin px-3 py-3"
      >
        {state.messages.length === 0 ? (
          <p className="pt-6 text-center text-xs text-muted-foreground">
            Ask about this project's policy, or ask for a change.
          </p>
        ) : (
          <div className="space-y-4">
            {state.messages.map((m) => (
              <Message key={m.id} message={m} />
            ))}
          </div>
        )}
      </div>
      <div className="border-t border-border p-2">
        <Composer
          onSubmit={(text) => {
            // Show the sent message and its answer, even after scrolling up.
            atBottomRef.current = true;
            return send(text, editor);
          }}
          agentName="the assistant"
          disabled={streaming}
          identity={
            <button
              type="button"
              onClick={newChat}
              disabled={state.messages.length === 0}
              className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
            >
              <RotateCcw className="size-3" />
              New chat
            </button>
          }
        />
      </div>
    </div>
  );
}

/** Memoised: a streamed token re-renders only the turn it lands in. */
const Message = memo(function Message({
  message,
}: {
  message: AssistantMessage;
}) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl bg-primary/15 px-3 py-2 text-xs whitespace-pre-wrap">
          {message.content}
        </div>
      </div>
    );
  }
  const turn = message.turn;
  return (
    <div className="flex items-start gap-2">
      <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-lg bg-action text-action-foreground">
        <Hexagon className="size-3" />
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        {turn?.tools.map((t) => (
          <ToolStep key={t.id} call={t} />
        ))}
        {message.content && (
          <div className="prose prose-sm prose-invert max-w-none text-xs">
            <Streamdown
              parseIncompleteMarkdown
              rehypePlugins={REHYPE_PLUGINS}
              disallowedElements={DISALLOWED}
              unwrapDisallowed
            >
              {message.content}
            </Streamdown>
          </div>
        )}
        {turn?.streaming && !message.content && <ThinkingGoo />}
        {turn?.error && <p className="text-xs text-deny">{turn.error}</p>}
      </div>
    </div>
  );
});

const STATE_LABEL: Record<AssistantToolCall["state"], string> = {
  started: "running",
  completed: "done",
  failed: "failed",
};

/** One tool call, its arguments and result as plain text: they carry file
 * contents and audit values, never markdown to render. */
const ToolStep = memo(function ToolStep({ call }: { call: AssistantToolCall }) {
  return (
    <details className="rounded border border-border bg-muted/30 text-[11px]">
      <summary className="cursor-pointer px-2 py-1 font-mono">
        {call.name}{" "}
        <span
          className={cn(
            "text-muted-foreground",
            call.state === "failed" && "text-deny",
          )}
        >
          · {STATE_LABEL[call.state]}
        </span>
      </summary>
      {call.argsText && <ToolText label="Arguments" text={call.argsText} />}
      {call.outputSummary && (
        <ToolText label="Result" text={call.outputSummary} />
      )}
    </details>
  );
});

function ToolText({ label, text }: { label: string; text: string }) {
  return (
    <div className="border-t border-border/60 px-2 py-1">
      <p className="text-muted-foreground">{label}</p>
      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono">
        {text}
      </pre>
    </div>
  );
}
