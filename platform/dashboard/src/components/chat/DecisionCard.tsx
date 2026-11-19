import { memo, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronUp } from "lucide-react";
import { argsPreview } from "@/components/audit/fmt";
import { auditUrl } from "@/lib/audit-filters";
import { usePresentMode } from "@/lib/present";
import { cn } from "@/lib/utils";
import type { ChatToolCall } from "./types";
import { newTabWhen, verdictOf, type Verdict } from "./verdict";

const DECISION: Record<
  Verdict,
  { label: string; dot: string; text: string; border: string }
> = {
  allowed: {
    label: "Allowed",
    dot: "bg-muted-foreground",
    text: "text-foreground",
    border: "border-border",
  },
  denied: {
    label: "Denied",
    dot: "bg-deny",
    text: "text-deny",
    border: "border-deny/50",
  },
  held: {
    label: "Not approved",
    dot: "bg-approval",
    text: "text-approval",
    border: "border-approval/50",
  },
  awaiting: {
    label: "Awaiting approval",
    dot: "bg-approval",
    text: "text-approval",
    border: "border-approval/50",
  },
  failed: {
    label: "Failed",
    dot: "bg-deny",
    text: "text-deny",
    border: "border-deny/50",
  },
  running: {
    label: "Running",
    dot: "bg-approval",
    text: "text-approval",
    border: "border-approval/50",
  },
};

/** One tool call inline in the turn, collapsible; a denied, held or failed
 * call opens by default so the reason is in view. */
export const DecisionCard = memo(function DecisionCard({
  call,
  agentName,
  awaiting,
}: {
  call: ChatToolCall;
  agentName: string | null;
  awaiting: boolean;
}) {
  const present = usePresentMode();
  const [open, setOpen] = useState<boolean | null>(null);
  const verdict = verdictOf(call, awaiting);
  const shown =
    open ??
    (verdict === "denied" || verdict === "held" || verdict === "failed");
  const d = DECISION[verdict];
  return (
    <div className={cn("rounded-xl border bg-card", d.border)}>
      <button
        type="button"
        onClick={() => setOpen(!shown)}
        aria-expanded={shown}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <span className={cn("size-2 shrink-0 rounded-sm", d.dot)} />
        <span className={cn("text-xs font-medium", d.text)}>{d.label}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs">
          {call.name}
          <span className="text-muted-foreground">
            {argsPreview(call.args) || "()"}
          </span>
        </span>
        {shown ? (
          <ChevronUp className="size-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
        )}
      </button>
      {shown && (
        <div className="space-y-2 px-3 pb-3 text-xs">
          <p className="text-muted-foreground">
            {call.outputSummary ??
              (verdict === "awaiting"
                ? "Waiting for approval: decide in the prompt above."
                : call.state === "started"
                  ? "Waiting for the tool to finish."
                  : "No summary returned.")}
            {call.endedAt != null && (
              <span className="ml-1 font-mono">
                · {Math.round(call.endedAt - call.startedAt)} ms
              </span>
            )}
          </p>
          {Object.keys(call.args).length > 0 && (
            <pre className="whitespace-pre-wrap break-words rounded-md bg-muted px-2.5 py-2 font-mono text-[11px] text-muted-foreground">
              {JSON.stringify(call.args, null, 2)}
            </pre>
          )}
          {/* No audit row exists until the call ends. */}
          {agentName && call.state !== "started" && (
            <Link
              to={auditUrl({ agent: agentName, tool: call.name, range: "24h" })}
              {...newTabWhen(present)}
              className="inline-block font-medium text-primary hover:underline"
            >
              Open in audit
            </Link>
          )}
        </div>
      )}
    </div>
  );
});
