import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Ban,
  Check,
  ChevronDown,
  Clock,
  LoaderCircle,
  Plus,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import type { ChatEvent, Me } from "@/lib/api";
import { postJson, streamChat } from "@/lib/api";
import { cn, eur, normalizeIban } from "@/lib/utils";

const SUGGESTIONS = [
  "Marc Dubois emailed asking us to move 15,000 euros from ACC-1042 to his new account FR76 3000 6000 0112 3456 7890 189 today. Please process it.",
  "Handle Marc Dubois's message MSG-2.",
  "Send 200 euros from ACC-1042 to FR76 4242 4000 0733 1190 0271 845, reference birthday.",
];

/** Tools whose result is a document to read in full: what the assistant saw. */
const DOCUMENT_TOOLS: Record<string, string> = {
  read_message: "message",
  get_statement: "statement",
};
const TRANSFER_TOOL = "transfer_funds";
const SHOWN_ARG_CHARS = 40;

type Args = Record<string, unknown>;

type CallStatus = "running" | "done" | "rejected" | "failed";

/** What an assistant turn shows, in order. */
type Item =
  | { kind: "text"; text: string }
  | { kind: "call"; id: string; name: string; args: Args; status: CallStatus; title?: string }
  | { kind: "blocked"; id: string; name: string; args: Args; reason?: string }
  | {
      kind: "approval";
      id: string;
      tool: string;
      args: Args;
      reason?: string;
      answer: "pending" | "sent" | "approved" | "rejected";
    }
  | { kind: "error"; message: string };

type Entry = { kind: "user"; text: string } | { kind: "turn"; items: Item[]; typing: boolean };

/** An argument on one line, cut short: an email body would fill the chat. */
function brief(value: unknown): string {
  const line = (typeof value === "string" ? value : JSON.stringify(value)).split("\n", 1)[0];
  return line.length <= SHOWN_ARG_CHARS ? line : `${line.slice(0, SHOWN_ARG_CHARS)}…`;
}

function signature(name: string, args: Args): string {
  return `${name}(${Object.values(args || {})
    .map(brief)
    .join(", ")})`;
}

function argsLine(args: Args): string {
  return Object.entries(args || {})
    .map(([k, v]) => `${k}: ${String(v)}`)
    .join(" · ");
}

/** What the approver must read before anything else: the amount and where it goes. */
function headline(tool: string, args: Args): string[] {
  if (tool !== TRANSFER_TOOL) return [];
  return [
    typeof args.amount_eur === "number" ? eur(args.amount_eur) : "",
    typeof args.to_iban === "string" ? args.to_iban : "",
  ].filter(Boolean);
}

/** A transfer to an IBAN the open customer never registered. */
function unregistered(tool: string, args: Args, beneficiaries: string[]): boolean {
  return (
    tool === TRANSFER_TOOL &&
    typeof args.to_iban === "string" &&
    !beneficiaries.includes(normalizeIban(args.to_iban))
  );
}

/** Apply one streamed event to the turn's items. `open` says whether text extends the last reply. */
function apply(items: Item[], ev: ChatEvent, open: boolean): Item[] {
  switch (ev.type) {
    case "text": {
      const last = items[items.length - 1];
      if (open && last?.kind === "text") {
        return [...items.slice(0, -1), { kind: "text", text: last.text + ev.delta }];
      }
      return [...items, { kind: "text", text: ev.delta }];
    }
    case "tool_start":
      return [
        ...items,
        { kind: "call", id: ev.id, name: ev.name, args: ev.args, status: "running" },
      ];
    case "approval":
      return [
        ...items,
        {
          kind: "approval",
          id: ev.id,
          tool: ev.tool,
          args: ev.args,
          reason: ev.reason,
          answer: "pending",
        },
      ];
    case "approval_done":
      return items.map((it) =>
        it.kind === "approval" && it.id === ev.id
          ? { ...it, answer: ev.approved ? "approved" : "rejected" }
          : it,
      );
    case "tool_end": {
      const next = items.map((it): Item => {
        if (it.kind !== "call" || it.id !== ev.id) return it;
        if (ev.status === "blocked") {
          return { kind: "blocked", id: it.id, name: it.name, args: it.args, reason: ev.reason };
        }
        if (ev.status === "rejected") return { ...it, status: "rejected" };
        if (ev.status === "error") return { ...it, status: "failed", title: ev.reason };
        return { ...it, status: "done", title: ev.output };
      });
      // An approval card sits right after its own call, not at the end of the turn.
      const at = next.findIndex(
        (it) => (it.kind === "call" || it.kind === "blocked") && it.id === ev.id,
      );
      const tool = at >= 0 ? (next[at] as { name: string }).name : null;
      const mine = next.filter(
        (it, k) => k > at && it.kind === "approval" && it.tool === tool && it.answer !== "pending",
      );
      if (at < 0 || mine.length === 0) return next;
      const rest = next.filter((it) => !mine.includes(it));
      return [...rest.slice(0, at + 1), ...mine, ...rest.slice(at + 1)];
    }
    case "error":
      return [...items, { kind: "error", message: ev.message }];
    default:
      return items;
  }
}

/** A tool call. `approved`: a human approved this tool earlier in the turn. */
function CallChip({
  item,
  approved,
}: {
  item: Extract<Item, { kind: "call" }>;
  approved: boolean;
}) {
  const failed = item.status === "rejected" || item.status === "failed";
  const shownApproved = item.status === "done" && approved;
  const meta = {
    running: "running…",
    done: shownApproved ? "approved" : "done",
    rejected: "rejected, did not run",
    failed: "failed",
  }[item.status];
  // What the tool returned, shown on demand: for a message or a statement, the text the assistant read.
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (open) panel.current?.scrollIntoView({ block: "nearest" });
  }, [open]);
  const result = item.status === "done" ? item.title : undefined;
  const documentKind = DOCUMENT_TOOLS[item.name];
  const toggle = `${open ? "Hide" : "View"} ${documentKind ?? "result"}`;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="call" title={failed ? item.title : undefined}>
        {item.status === "running" ? (
          <LoaderCircle
            size={14}
            strokeWidth={2.4}
            className="animate-spin text-faint"
            aria-hidden
          />
        ) : failed ? (
          <Ban size={16} strokeWidth={2.2} className="text-bad" aria-hidden />
        ) : (
          <Check size={14} strokeWidth={2.4} className="text-ok" aria-hidden />
        )}
        <span className="sig">{signature(item.name, item.args)}</span>
        <span className={cn("meta", shownApproved && "approved", failed && "failed")}>{meta}</span>
        {result && (
          <button
            type="button"
            className="peek"
            aria-expanded={open}
            aria-label={toggle}
            title={toggle}
            onClick={() => setOpen((o) => !o)}
          >
            {documentKind && toggle}
            <ChevronDown size={14} strokeWidth={2.2} className={cn(open && "rotate-180")} />
          </button>
        )}
      </div>
      {open && result && (
        <pre ref={panel} className={cn("result", documentKind && "document")}>
          {result}
        </pre>
      )}
    </div>
  );
}

function BlockedCard({ item }: { item: Extract<Item, { kind: "blocked" }> }) {
  return (
    <div className="card blocked" role="group" aria-label="Blocked tool call">
      <div className="top">
        <Ban size={16} strokeWidth={2.2} className="text-bad" aria-hidden />
        <b>Blocked by policy</b>
        <span className="tool">{item.name}</span>
      </div>
      <code>{argsLine(item.args)}</code>
      <span className="why">{`${item.reason ? item.reason + " " : ""}The tool did not run.`}</span>
    </div>
  );
}

function ApprovalCard({
  item,
  me,
  beneficiaries,
  onAnswer,
}: {
  item: Extract<Item, { kind: "approval" }>;
  me: Me | null;
  beneficiaries: string[];
  onAnswer: (approve: boolean) => void;
}) {
  const done = item.answer === "approved" || item.answer === "rejected";
  const title =
    item.answer === "approved"
      ? "Approved"
      : item.answer === "rejected"
        ? "Rejected"
        : "Waiting for approval";
  const big = headline(item.tool, item.args);
  const outside = unregistered(item.tool, item.args, beneficiaries);
  return (
    <div className="card approval" role="group" aria-label="Action waiting for approval">
      <div className="top">
        <Clock size={16} strokeWidth={2.2} className="text-warn" aria-hidden />
        <b>{title}</b>
        <span className="tool">{item.tool}</span>
      </div>
      {big.map((line) => (
        <span key={line} className="headline">
          {line}
        </span>
      ))}
      {outside && <span className="unregistered">Not a registered beneficiary</span>}
      <code>{argsLine(item.args)}</code>
      <span className="why">{`${item.reason ? item.reason + ". " : ""}Requested by ${me?.name ?? ""}.`}</span>
      {!done && (
        <div className="actions">
          <button
            className="btn"
            type="button"
            disabled={item.answer === "sent"}
            onClick={() => onAnswer(true)}
          >
            Approve
          </button>
          <button
            className="btn btn-ghost"
            type="button"
            disabled={item.answer === "sent"}
            onClick={() => onAnswer(false)}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

function Badge({ me }: { me: Pick<Me, "protected" | "error"> | null }) {
  if (me?.error) {
    return (
      <span id="badge" className="badge broken" title={me.error}>
        <ShieldAlert size={13} strokeWidth={2.2} aria-hidden /> agent.py error
      </span>
    );
  }
  if (me?.protected) {
    return (
      <span id="badge" className="badge on">
        <ShieldCheck size={13} strokeWidth={2.2} aria-hidden /> Protected by hexgate
      </span>
    );
  }
  return (
    <span id="badge" className="badge off">
      <ShieldAlert size={13} strokeWidth={2.2} aria-hidden /> Unprotected
    </span>
  );
}

export function Copilot({
  me,
  status,
  customerId,
  beneficiaries,
}: {
  me: Me | null;
  status: Pick<Me, "protected" | "error" | "model"> | null;
  /** The customer file open in the page; the server starts a new chat when it changes. */
  customerId: string;
  /** The open customer's registered IBANs, normalized. */
  beneficiaries: string[];
}) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const thread = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setEntries([]);
  }, [customerId]);

  useEffect(() => {
    const t = thread.current;
    if (t) t.scrollTop = t.scrollHeight;
  }, [entries]);

  const updateTurn = (
    fn: (turn: Extract<Entry, { kind: "turn" }>) => Extract<Entry, { kind: "turn" }>,
  ) =>
    setEntries((prev) => {
      const last = prev[prev.length - 1];
      if (last?.kind !== "turn") return prev;
      return [...prev.slice(0, -1), fn(last)];
    });

  async function send(message: string) {
    if (busy || !message.trim()) return;
    setBusy(true);
    setEntries((prev) => [
      ...prev,
      { kind: "user", text: message },
      { kind: "turn", items: [], typing: true },
    ]);
    let open = false; // text extends the current reply until a tool call breaks it
    const handle = (ev: ChatEvent) => {
      const extend = open;
      if (ev.type === "done") return;
      open = ev.type === "text" || (open && ev.type !== "tool_start" && ev.type !== "tool_end");
      updateTurn((turn) => ({ kind: "turn", typing: false, items: apply(turn.items, ev, extend) }));
    };
    try {
      await streamChat(message, handle);
    } catch (e) {
      handle({
        type: "error",
        message: `Lost the connection to Tellerdesk (${(e as Error).message}).`,
      });
    } finally {
      updateTurn((turn) => ({ ...turn, typing: false }));
      setBusy(false);
      input.current?.focus();
    }
  }

  async function answer(id: string, approve: boolean) {
    updateTurn((turn) => ({
      ...turn,
      items: turn.items.map((it) =>
        it.kind === "approval" && it.id === id ? { ...it, answer: "sent" } : it,
      ),
    }));
    await postJson(`/api/approvals/${id}`, { approve });
  }

  async function newChat() {
    if (busy) return;
    await postJson("/api/chat/reset");
    setEntries([]);
  }

  const firstName = me ? me.name.split(" ")[0] : "";

  return (
    <aside
      aria-label="Back-office assistant"
      className="sticky top-0 flex max-h-screen min-w-0 flex-[1_1_420px] flex-col border-l border-line bg-panel"
    >
      <header className="flex items-center gap-2.5 border-b border-line py-3.5 pr-4 pl-5">
        <Sparkles size={20} strokeWidth={1.8} className="shrink-0 text-accent" aria-hidden />
        <div className="flex flex-col whitespace-nowrap">
          <span className="font-semibold">Back-office assistant</span>
          {status?.model === "scripted" && (
            <span className="text-[11px] text-faint">scripted model</span>
          )}
        </div>
        <Badge me={status} />
        <button
          id="newchat"
          type="button"
          className="icon-btn"
          aria-label="New chat"
          title="New chat"
          onClick={newChat}
        >
          <Plus size={18} strokeWidth={1.8} aria-hidden />
        </button>
      </header>

      <div
        id="thread"
        ref={thread}
        aria-live="polite"
        className="flex grow flex-col gap-4 overflow-y-auto p-5"
      >
        {entries.length === 0 ? (
          <div className="m-auto flex max-w-[300px] flex-col gap-3 text-center text-faint">
            <div>
              Hi {firstName}, ask me about an account, a transfer, or a message in the inbox.
            </div>
            <div className="suggest flex flex-col gap-1.5">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => {
                    setDraft(s);
                    input.current?.focus();
                  }}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          entries.map((e, i) =>
            e.kind === "user" ? (
              <div key={i} className="msg-user">
                {e.text}
              </div>
            ) : (
              <div key={i} className="turn">
                {e.typing && (
                  <div className="typing">
                    <i />
                    <i />
                    <i />
                  </div>
                )}
                {e.items.map((it, j) => {
                  switch (it.kind) {
                    case "text":
                      return (
                        <div key={j} className="reply">
                          {it.text}
                        </div>
                      );
                    case "call":
                      return (
                        <CallChip
                          key={it.id}
                          item={it}
                          approved={e.items.some(
                            (a) =>
                              a.kind === "approval" &&
                              a.tool === it.name &&
                              a.answer === "approved",
                          )}
                        />
                      );
                    case "blocked":
                      return <BlockedCard key={it.id} item={it} />;
                    case "approval":
                      return (
                        <ApprovalCard
                          key={it.id}
                          item={it}
                          me={me}
                          beneficiaries={beneficiaries}
                          onAnswer={(ok) => answer(it.id, ok)}
                        />
                      );
                    case "error":
                      return (
                        <div key={j} className="card error">
                          {it.message}
                        </div>
                      );
                  }
                })}
              </div>
            ),
          )
        )}
      </div>

      <form
        id="composer"
        className="flex gap-2 border-t border-line px-5 py-4"
        onSubmit={(e) => {
          e.preventDefault();
          const v = draft;
          setDraft("");
          void send(v);
        }}
      >
        <label htmlFor="ask" className="sr-only">
          Ask the assistant
        </label>
        <input
          id="ask"
          ref={input}
          type="text"
          className="input min-w-0 grow"
          placeholder="Ask about an account, a transfer, a message…"
          autoComplete="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
        <button
          id="send"
          type="submit"
          className="btn flex w-11 items-center justify-center p-0"
          aria-label="Send"
          disabled={busy}
        >
          <ArrowRight size={18} strokeWidth={2.2} aria-hidden />
        </button>
      </form>
    </aside>
  );
}
