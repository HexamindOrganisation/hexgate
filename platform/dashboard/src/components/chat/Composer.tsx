import { useState } from "react";
import { ArrowUp } from "lucide-react";

/** Holds its own text, so a keystroke re-renders the box, not the transcript. */
export function Composer({
  onSubmit,
  agentName,
  identity,
  disabled = false,
}: {
  /** A promise that resolves false means the message was not sent: the box
   * gets its text back, unless something new has been typed since. */
  onSubmit: (text: string) => void | Promise<boolean>;
  agentName: string | null;
  identity: React.ReactNode;
  /** Keeps the text and refuses to send, e.g. while an answer streams. */
  disabled?: boolean;
}) {
  const [value, setValue] = useState("");
  const text = value.trim();
  const canSend = text !== "" && !disabled;
  const send = () => {
    if (!canSend) return;
    const draft = value;
    setValue("");
    void Promise.resolve(onSubmit(text)).then((sent) => {
      if (sent === false) setValue((now) => (now === "" ? draft : now));
    });
  };
  return (
    <div className="rounded-2xl border border-border bg-card p-3 shadow-sm transition-colors focus-within:border-primary/60">
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          // Enter confirming an IME candidate isn't a send (Safari reports
          // it as keyCode 229 rather than isComposing).
          const composing =
            e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229;
          if (e.key === "Enter" && !e.shiftKey && !composing) {
            e.preventDefault();
            send();
          }
        }}
        rows={1}
        placeholder={`Message ${agentName ?? "the agent"}…`}
        aria-label="Message the agent"
        className="block min-h-12 max-h-48 w-full resize-none bg-transparent px-1 text-sm focus:outline-none [field-sizing:content]"
      />
      <div className="mt-2 flex items-center justify-between gap-2">
        {identity}
        <button
          type="button"
          onClick={send}
          disabled={!canSend}
          aria-label="Send"
          className="grid size-9 place-items-center rounded-full bg-action text-action-foreground transition-all hover:bg-action/85 disabled:scale-90 disabled:bg-muted disabled:text-muted-foreground"
        >
          <ArrowUp className="size-4" />
        </button>
      </div>
    </div>
  );
}
