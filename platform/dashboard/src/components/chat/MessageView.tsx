import { memo } from "react";
import { Streamdown } from "streamdown";
import { Hexagon } from "lucide-react";
import { cn } from "@/lib/utils";
import { DecisionCard } from "./DecisionCard";
import { ThinkingGoo } from "./ThinkingGoo";
import type { ChatMessageData } from "./types";
import { callKey } from "./verdict";

/** One message. Memoised: a streamed token re-renders only the turn it
 * lands in, since untouched messages keep their identity. */
export const MessageView = memo(function MessageView({
  message,
  agentName,
  present,
  awaiting,
}: {
  message: ChatMessageData;
  agentName: string | null;
  present: boolean;
  awaiting: ReadonlySet<string>;
}) {
  if (message.role === "user") {
    return (
      <div className="pg-enter flex justify-end">
        <div
          className={cn(
            "max-w-[85%] rounded-2xl px-4 py-2.5 text-sm",
            present ? "bg-primary/15" : "bg-muted",
          )}
        >
          <div className="whitespace-pre-wrap">{message.content}</div>
        </div>
      </div>
    );
  }

  const turn = message.turn;
  const steps = turn && (turn.reasoning || turn.tools.length) ? turn : null;
  return (
    <div className="pg-enter flex items-start gap-3">
      <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-action text-action-foreground">
        <Hexagon className="size-3.5" />
      </span>
      <div className="min-w-0 flex-1 space-y-3 pt-0.5">
        {/* Intermediate steps: the agent's reasoning + each tool call, shown as
            a compact timeline before the final answer. */}
        {steps && (
          <div className="space-y-2">
            {steps.reasoning !== "" && (
              <div className="border-l-2 border-border pl-3 text-xs italic text-muted-foreground">
                {steps.reasoning}
              </div>
            )}
            {steps.tools.map((t) => (
              <DecisionCard
                key={t.id}
                call={t}
                agentName={agentName}
                // Only the turn that's running can be at the prompt.
                awaiting={
                  !!steps.streaming &&
                  t.state === "started" &&
                  awaiting.has(callKey(t.name, t.args))
                }
              />
            ))}
          </div>
        )}
        {message.content && (
          <div className="prose prose-sm prose-invert max-w-none text-sm">
            <Streamdown parseIncompleteMarkdown>{message.content}</Streamdown>
            {turn?.streaming && <span className="pg-caret" aria-hidden />}
          </div>
        )}
        {turn?.streaming && !message.content && <ThinkingGoo />}
        {turn?.error && (
          <div className="text-xs text-deny">error: {turn.error}</div>
        )}
      </div>
    </div>
  );
});
