import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Streamdown } from "streamdown";
import {
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  CircleDashed,
  RefreshCcw,
  RadioReceiver,
  ShieldAlert,
  UserCog,
  Wrench,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  usePlayground,
  type ApprovalRequestEvent,
  type ChatMessage,
  type ToolCall,
} from "@/lib/playground";
import { DocsLink } from "@/components/DocsLink";
import { DOC_PATHS } from "@/lib/docs";
import { api, type AgentRead } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { parseRolesFromPolicy } from "@/lib/policy";
import { useResolvedPolicy } from "@/lib/policy_files";
import { cn } from "@/lib/utils";

export function PlaygroundPage() {
  const scope = useProjectScoped();
  if (scope.status === "no-project") {
    return <NoProjectEmptyState resource="playground" />;
  }
  if (scope.status === "loading" || !scope.projectId) {
    // Brief while the bootstrap effect picks a default project. The live UI
    // opens a WS keyed on projectId — don't mount it until we have a real one,
    // else the reconnect loop would spam ``ws://…/v1/projects//chat``.
    return (
      <div className="grid h-full place-items-center text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }
  return <PlaygroundLive projectId={scope.projectId} />;
}

function PlaygroundLive({ projectId }: { projectId: string }) {
  const { state, sendChat, reset, respondToApproval } = usePlayground({
    projectId,
  });
  const [composer, setComposer] = useState("");
  const [agent, setAgent] = useState<AgentRead | null>(null);
  // A set, not a scalar: the enforcer evaluates every role the caller carries,
  // so the playground has to be able to reproduce a multi-role caller.
  const [activeRoles, setActiveRoles] = useState<string[]>([]);
  const transcriptRef = useRef<HTMLDivElement>(null);

  // Fetch the serving agent so we know which roles are available. Roles are a
  // per-agent concept today (M1); a later global role registry moves this hook.
  useEffect(() => {
    if (!state.agentName) {
      setAgent(null);
      return;
    }
    let cancelled = false;
    api
      .getAgent(state.agentName, projectId)
      .then((a) => {
        if (!cancelled) setAgent(a);
      })
      .catch(() => {
        if (!cancelled) setAgent(null);
      });
    return () => {
      cancelled = true;
    };
  }, [state.agentName, projectId]);

  // Roles for the "Acting as" picker. A compose (modular) project has no
  // per-agent role registry in the classic policy_yaml — its roles come from
  // resolving the agent's compose policy — so prefer those, and fall back to the
  // classic policy_yaml for a non-modular agent. (A classic project 422s on
  // resolve → empty data → the fallback.)
  const resolved = useResolvedPolicy(
    projectId,
    undefined,
    state.agentName ?? undefined,
  );
  const roleOptions = useMemo(() => {
    const composeRoles = Object.keys(resolved.data ?? {}).sort();
    if (composeRoles.length > 0) return composeRoles;
    return agent ? parseRolesFromPolicy(agent.policy_yaml) : [];
  }, [resolved.data, agent]);

  // On an agent switch, keep whichever roles the new one still defines, else
  // fall back to its first role.
  useEffect(() => {
    if (roleOptions.length === 0) {
      setActiveRoles([]);
      return;
    }
    setActiveRoles((prev) => {
      const kept = prev.filter((r) => roleOptions.includes(r));
      return kept.length ? kept : [roleOptions[0]];
    });
  }, [roleOptions]);

  useEffect(() => {
    const el = transcriptRef.current;
    if (!el) return;
    // Stick to the bottom only when the user is already near it — otherwise a
    // per-token stream would yank them down while they read an earlier turn.
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) el.scrollTo({ top: el.scrollHeight });
  }, [state.messages]);

  function submit() {
    const text = composer.trim();
    if (!text) return;
    sendChat(text, activeRoles.length ? { roles: activeRoles } : undefined);
    setComposer("");
  }

  const empty = state.messages.length === 0;

  return (
    <div className="-mx-8 -my-6 relative flex h-screen flex-col overflow-hidden bg-background">
      <GooFilter />

      {/* Status, mixed into the top corners over the chat — no side panel. */}
      <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex items-start justify-between gap-3 px-4 py-3">
        <AgentStatus
          agentName={state.agentName}
          online={state.agentOnline}
          relayConnected={state.connected}
        />
        <div className="pointer-events-auto flex items-center gap-2">
          {roleOptions.length > 0 && (
            <ActingAsControl
              roleOptions={roleOptions}
              activeRoles={activeRoles}
              setActiveRoles={setActiveRoles}
            />
          )}
          <button
            type="button"
            onClick={reset}
            disabled={empty}
            title="Reset session"
            className="grid size-8 place-items-center rounded-full border border-border/60 bg-card/70 text-muted-foreground backdrop-blur transition-colors hover:bg-accent hover:text-foreground disabled:opacity-40"
          >
            <RefreshCcw className="size-3.5" />
          </button>
        </div>
      </div>

      {/* Transcript */}
      <div
        ref={transcriptRef}
        className="scrollbar-thin flex-1 overflow-y-auto"
      >
        <div className="mx-auto w-full max-w-2xl px-4 pb-6 pt-20">
          {empty ? (
            <EmptyState online={state.agentOnline} />
          ) : (
            <div className="space-y-6">
              {state.messages.map((m) => (
                <MessageView key={m.id} message={m} />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Composer + inline approvals, centred like the transcript. */}
      <div className="relative z-10 border-t border-border/60 bg-background/80 backdrop-blur">
        <div className="mx-auto w-full max-w-2xl px-4 py-4">
          {/* Persist the "how to bring an agent back" hint mid-session, not just
              on the empty state — else a mid-session disconnect looks silent. */}
          {!state.agentOnline && !empty && (
            <div className="mb-3 rounded-lg border border-approval/40 bg-approval/5 px-3 py-2 text-xs text-muted-foreground">
              <span className="font-medium text-approval">Agent offline.</span>{" "}
              Run{" "}
              <span className="font-mono text-foreground">hexgate serve</span>{" "}
              to bring it back — messages you send will wait.
            </div>
          )}
          {state.pendingApprovals.length > 0 && (
            <ApprovalPromptStack
              pending={state.pendingApprovals}
              onDecide={respondToApproval}
            />
          )}
          <Composer value={composer} onChange={setComposer} onSubmit={submit} />
        </div>
      </div>

      {/* Streamed decisions as a floating deck — hidden while an approval is
          pending so its pile can't overlap the Approve/Deny buttons. */}
      {state.pendingApprovals.length === 0 && (
        <DecisionDeck decisions={state.decisions} />
      )}
    </div>
  );
}

// ── Top-corner status ───────────────────────────────────────────────────────

function AgentStatus({
  agentName,
  online,
  relayConnected,
}: {
  agentName: string | null;
  online: boolean;
  relayConnected: boolean;
}) {
  const pill =
    "pointer-events-auto flex items-center gap-2 rounded-full border border-border/60 bg-card/70 px-3 py-1.5 text-xs backdrop-blur";
  if (!agentName) {
    return (
      <div className={cn(pill, "text-muted-foreground")}>
        <RadioReceiver className="size-3.5 text-approval" />
        no agent serving
      </div>
    );
  }
  return (
    <Link
      to="/agents"
      className={cn(pill, "transition-colors hover:bg-accent")}
    >
      <Bot className="size-3.5 text-primary" />
      <span className="max-w-[40vw] truncate font-mono font-medium">
        {agentName}
      </span>
      <span className="flex items-center gap-1.5">
        {online ? (
          <>
            <span className="relative inline-flex size-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-allow opacity-60" />
              <span className="relative inline-flex size-2 rounded-full bg-allow" />
            </span>
            <span className="text-allow">connected</span>
          </>
        ) : (
          <>
            <span className="size-2 rounded-full bg-muted-foreground" />
            <span className="text-muted-foreground">offline</span>
          </>
        )}
      </span>
      {!relayConnected && (
        <span className="text-[10px] text-approval">· reconnecting…</span>
      )}
    </Link>
  );
}

function ActingAsControl({
  roleOptions,
  activeRoles,
  setActiveRoles,
}: {
  roleOptions: string[];
  activeRoles: string[];
  setActiveRoles: React.Dispatch<React.SetStateAction<string[]>>;
}) {
  const label =
    activeRoles.length === 0
      ? "default"
      : activeRoles.length === 1
        ? activeRoles[0]
        : `${activeRoles[0]} +${activeRoles.length - 1}`;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1.5 rounded-full border border-border/60 bg-card/70 px-3 py-1.5 text-xs backdrop-blur transition-colors hover:bg-accent"
        >
          <UserCog className="size-3.5 text-primary" />
          <span className="text-muted-foreground">acting as</span>
          <span className="max-w-[24vw] truncate font-mono font-medium">
            {label}
          </span>
          <ChevronDown className="size-3 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-60 p-2">
        <div className="mb-1 px-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          Acting as
        </div>
        <div className="flex flex-col gap-0.5">
          {roleOptions.map((role) => (
            <label
              key={role}
              className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 font-mono text-sm hover:bg-accent"
            >
              <input
                type="checkbox"
                checked={activeRoles.includes(role)}
                onChange={(e) =>
                  setActiveRoles((prev) =>
                    e.target.checked
                      ? // Re-derive so the emitted order is the policy's, not
                        // the click order.
                        roleOptions.filter(
                          (r) => r === role || prev.includes(r),
                        )
                      : prev.filter((r) => r !== role),
                  )
                }
                className="size-3.5 accent-primary"
              />
              {role}
            </label>
          ))}
        </div>
        <p className="mt-2 border-t border-border/60 px-1 pt-2 text-[11px] leading-snug text-muted-foreground">
          {activeRoles.length ? (
            <>
              Each turn attenuates the token with{" "}
              <span className="font-mono">
                {activeRoles.map((r) => `role("${r}")`).join(", ")}
              </span>
              .{" "}
              {activeRoles.length > 1
                ? "The most permissive outcome across roles wins."
                : "That role's bundle decides which tools fire."}
            </>
          ) : (
            <>
              No role — the turn runs unroled and the{" "}
              <span className="font-mono">default</span> policy decides.
            </>
          )}
        </p>
      </PopoverContent>
    </Popover>
  );
}

function EmptyState({ online }: { online: boolean }) {
  return (
    <div className="grid min-h-[50vh] place-items-center text-center">
      <div className="max-w-sm space-y-2">
        <div className="mx-auto grid size-12 place-items-center rounded-2xl bg-primary/10 text-primary">
          <Bot className="size-6" />
        </div>
        <p className="text-sm text-muted-foreground">
          Ask the agent to do something to start a session.
        </p>
        {!online && (
          <p className="text-xs text-muted-foreground">
            No agent connected — run{" "}
            <span className="font-mono text-foreground">hexgate serve</span> to
            expose one. Responses will wait until it does.
          </p>
        )}
        <div className="flex justify-center">
          <DocsLink path={DOC_PATHS.playground} label="Playground docs" />
        </div>
      </div>
    </div>
  );
}

// ── Composer ────────────────────────────────────────────────────────────────

function Composer({
  value,
  onChange,
  onSubmit,
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="relative flex items-center">
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSubmit();
          }
        }}
        placeholder="Ask the agent to do something…"
        aria-label="Message the agent"
        className="h-12 w-full rounded-full border border-border bg-card pl-5 pr-14 text-sm shadow-sm transition-colors focus:outline-none focus-visible:border-primary/60 focus-visible:ring-2 focus-visible:ring-primary/20"
      />
      <button
        type="button"
        onClick={onSubmit}
        disabled={!value.trim()}
        aria-label="Send"
        className="absolute right-2 grid size-9 place-items-center rounded-full bg-primary text-primary-foreground transition-all hover:bg-primary/90 disabled:scale-90 disabled:bg-muted disabled:text-muted-foreground"
      >
        <ArrowUp className="size-4" />
      </button>
    </div>
  );
}

// ── Transcript ──────────────────────────────────────────────────────────────

function MessageView({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="pg-enter flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-primary px-4 py-2.5 text-sm text-primary-foreground">
          <div className="whitespace-pre-wrap">{message.content}</div>
        </div>
      </div>
    );
  }

  const turn = message.turn;
  const hasSteps = !!turn && (turn.reasoning !== "" || turn.tools.length > 0);
  return (
    <div className="pg-enter flex items-start gap-3">
      <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-full bg-primary/10">
        <Bot className="size-3.5 text-primary" />
      </span>
      <div className="min-w-0 flex-1 space-y-3 pt-0.5">
        {/* Intermediate steps: the agent's reasoning + each tool call, shown as
            a compact timeline before the final answer. */}
        {hasSteps && (
          <div className="space-y-2">
            {turn!.reasoning !== "" && (
              <div className="border-l-2 border-border pl-3 text-xs italic text-muted-foreground">
                {turn!.reasoning}
              </div>
            )}
            {turn!.tools.map((t) => (
              <ToolStep key={t.id} call={t} />
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
}

/** The hexkit "goo" loader: a shimmering label over three dots that fuse into a
 * liquid blob via the #pg-goo SVG filter. Shown while the turn is still
 * thinking and no tokens have streamed yet. */
function ThinkingGoo() {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="pg-shimmer text-xs font-medium">Thinking…</span>
      <div className="pg-goo" role="status" aria-label="Thinking">
        <span className="d1" />
        <span className="d2" />
        <span className="d3" />
      </div>
    </div>
  );
}

/** The once-per-page SVG filter that gels the three dots into a metaball. */
function GooFilter() {
  return (
    <svg
      width="0"
      height="0"
      aria-hidden
      focusable="false"
      className="absolute"
    >
      <defs>
        <filter id="pg-goo" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur in="SourceGraphic" stdDeviation="4" result="b" />
          <feColorMatrix
            in="b"
            mode="matrix"
            values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9"
          />
        </filter>
      </defs>
    </svg>
  );
}

/** The verdict icon as an element (not a component variable — keeps the dynamic
 * pick out of render-time component identity). */
function stateIcon(state: ToolCall["state"], className: string) {
  if (state === "completed") return <Check className={className} />;
  if (state === "failed") return <X className={className} />;
  return <CircleDashed className={className} />;
}

function ToolStep({ call }: { call: ToolCall }) {
  const variant: "allow" | "deny" | "approval" =
    call.state === "completed"
      ? "allow"
      : call.state === "failed"
        ? "deny"
        : "approval";
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card/50">
      <div className="flex items-center gap-2 px-3 py-2">
        <Wrench className="size-3.5 text-muted-foreground" />
        <span className="font-mono text-xs">{call.name}</span>
        <Badge variant={variant} className="ml-auto">
          {stateIcon(call.state, "size-3")}
          {call.state}
        </Badge>
      </div>
      {Object.keys(call.args).length > 0 && (
        <pre className="whitespace-pre-wrap break-words border-t border-border px-3 py-2 font-mono text-[11px] text-muted-foreground">
          {JSON.stringify(call.args, null, 2)}
        </pre>
      )}
      {call.outputSummary && (
        <div className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
          → {call.outputSummary}
        </div>
      )}
    </div>
  );
}

// ── Floating decision deck ──────────────────────────────────────────────────
//
// The streamed policy decisions collapse into a small pile at the bottom-right;
// hovering (or focusing) the deck fans the full list up with per-decision
// details, so the decisions never claim a permanent panel.

function DecisionDeck({ decisions }: { decisions: ToolCall[] }) {
  // Visibility is JS-driven so it works past hover: the mouse opens it, a
  // click/tap pins it open (touch), and the handle is keyboard-operable.
  const [pinned, setPinned] = useState(false);
  const [hover, setHover] = useState(false);
  if (decisions.length === 0) return null;
  const shown = pinned || hover;
  const pile = decisions.slice(-3); // top few, most recent last
  return (
    <div
      className="absolute bottom-24 right-4 z-20 sm:bottom-6 sm:right-6"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {/* Expanded list — fans up when the deck is open. Focusable so keyboard
          users can scroll the list once it's pinned open. */}
      <div
        tabIndex={shown ? 0 : -1}
        className={cn(
          "absolute bottom-full right-0 mb-2 max-h-[55vh] w-[320px] origin-bottom-right overflow-y-auto rounded-xl border border-border bg-card/95 p-2 shadow-xl backdrop-blur transition-all duration-200 scrollbar-thin",
          shown
            ? "pointer-events-auto scale-100 opacity-100"
            : "pointer-events-none scale-95 opacity-0",
        )}
      >
        <div className="mb-1 flex items-center gap-1.5 px-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          <ShieldAlert className="size-3" />
          Decisions
        </div>
        <div className="flex flex-col gap-1">
          {decisions
            .slice()
            .reverse()
            .map((d) => (
              <DecisionCard key={d.id} call={d} />
            ))}
        </div>
      </div>

      {/* The collapsed pile — the open handle (hover or click/tap). */}
      <button
        type="button"
        aria-label={`${decisions.length} policy decisions`}
        aria-expanded={shown}
        onClick={() => setPinned((p) => !p)}
        className="relative block h-[52px] w-[216px]"
      >
        {pile.map((d, i) => {
          const depth = pile.length - 1 - i; // 0 = top card
          return (
            <div
              key={d.id}
              className={cn(
                "absolute inset-x-0 transition-transform duration-200",
                shown && "translate-y-1",
              )}
              style={{
                bottom: depth * 5,
                transform: `scale(${1 - depth * 0.05})`,
                zIndex: pile.length - depth,
                opacity: 1 - depth * 0.18,
              }}
            >
              <MiniDecision call={d} />
            </div>
          );
        })}
        <span className="absolute -top-2 right-1 z-10 rounded-full border border-border bg-background px-1.5 text-[10px] font-medium text-muted-foreground shadow-sm">
          {decisions.length}
        </span>
      </button>
    </div>
  );
}

function verdictColor(state: ToolCall["state"]) {
  return state === "completed"
    ? "text-allow"
    : state === "failed"
      ? "text-deny"
      : "text-approval";
}

function MiniDecision({ call }: { call: ToolCall }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-xs shadow-sm">
      {stateIcon(call.state, cn("size-3.5 shrink-0", verdictColor(call.state)))}
      <span className="flex-1 truncate font-mono">{call.name}</span>
    </div>
  );
}

function DecisionCard({ call }: { call: ToolCall }) {
  return (
    <div className="rounded-lg border border-border bg-background/60 px-2.5 py-2 text-xs">
      <div className="flex items-center gap-2">
        {stateIcon(
          call.state,
          cn("size-3.5 shrink-0", verdictColor(call.state)),
        )}
        <span className="flex-1 truncate font-mono">{call.name}</span>
        <span className={cn("font-medium", verdictColor(call.state))}>
          {call.state}
        </span>
        <span className="tabular-nums text-[10px] text-muted-foreground">
          {call.endedAt ? `${call.endedAt - call.startedAt}ms` : "…"}
        </span>
      </div>
      {Object.keys(call.args).length > 0 && (
        <pre className="mt-1.5 max-h-24 overflow-y-auto whitespace-pre-wrap break-words rounded bg-muted/60 p-1.5 font-mono text-[10px] leading-snug text-muted-foreground scrollbar-thin">
          {JSON.stringify(call.args, null, 2)}
        </pre>
      )}
      {call.outputSummary && (
        <div className="mt-1 text-[10px] text-muted-foreground">
          → {call.outputSummary}
        </div>
      )}
    </div>
  );
}

// ── Approval prompts ────────────────────────────────────────────────────────
//
// The prompt renders INLINE above the composer (not as a modal) so the user
// sees it in the flow of the conversation, not as a context-stealing overlay.
// When N > 1 concurrent approvals fire (parallel tool calls via asyncio.gather
// on the serve side), the stack renders each as its own row with its own decide
// buttons — resolving one doesn't affect the others.

interface ApprovalPromptStackProps {
  pending: ApprovalRequestEvent[];
  onDecide: (decision_id: string, allowed: boolean) => boolean;
}

function ApprovalPromptStack({ pending, onDecide }: ApprovalPromptStackProps) {
  // One 1 Hz ticker for the whole stack — passed down as ``now`` so every card
  // renders in lockstep from a single interval.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="mb-3 space-y-2 rounded-xl border border-approval/40 bg-approval/5 p-3">
      <div className="flex items-center gap-2 text-xs font-medium text-approval">
        <ShieldAlert className="size-3.5" />
        {pending.length === 1
          ? "1 approval pending"
          : `${pending.length} approvals pending`}
      </div>
      {pending.map((req) => (
        <ApprovalPromptCard
          key={req.decision_id}
          request={req}
          onDecide={onDecide}
          now={now}
        />
      ))}
    </div>
  );
}

interface ApprovalPromptCardProps {
  request: ApprovalRequestEvent;
  onDecide: (decision_id: string, allowed: boolean) => boolean;
  now: number;
}

function ApprovalPromptCard({
  request,
  onDecide,
  now,
}: ApprovalPromptCardProps) {
  const deadline = useMemo(
    () => new Date(request.expires_at).getTime(),
    [request.expires_at],
  );
  const remaining = Math.max(0, Math.floor((deadline - now) / 1000));
  const urgency =
    remaining <= 10
      ? "text-deny"
      : remaining <= 30
        ? "text-approval"
        : "text-muted-foreground";

  const argsPretty = useMemo(
    () => JSON.stringify(request.arguments, null, 2),
    [request.arguments],
  );

  return (
    <div className="rounded-lg border border-approval/40 bg-background/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-sm">
            <Wrench className="size-3.5 shrink-0 text-approval" />
            <span className="truncate font-mono font-medium">
              {request.tool_name}
            </span>
            {/* The badge names the role that GATED the call; the title carries
                the full set. Absent `roles` means an older `hexgate serve`. */}
            {request.role && (
              <Badge
                variant="outline"
                className="shrink-0 font-mono text-[10px]"
                title={
                  request.roles?.length
                    ? `gated by ${request.role} · caller carried ${request.roles.join(", ")}`
                    : undefined
                }
              >
                {request.role}
                {request.roles && request.roles.length > 1 && (
                  <span className="ml-1 opacity-60">
                    +{request.roles.length - 1}
                  </span>
                )}
              </Badge>
            )}
          </div>
          {request.reason && (
            <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
              {request.reason}
            </p>
          )}
        </div>
        <span
          className={cn("shrink-0 font-mono text-[10px] tabular-nums", urgency)}
        >
          {remaining > 0 ? `${remaining}s` : "expired"}
        </span>
      </div>
      <pre className="mt-2 max-h-40 overflow-y-auto rounded bg-muted/60 p-2 font-mono text-[11px] leading-snug scrollbar-thin">
        {argsPretty}
      </pre>
      <div className="mt-2 flex items-center justify-end gap-2">
        <Button
          size="sm"
          variant="outline"
          className="h-7 gap-1.5 border-deny/40 text-xs hover:bg-deny/10 hover:text-deny"
          onClick={() => onDecide(request.decision_id, false)}
        >
          <X className="size-3" />
          Deny
        </Button>
        <Button
          size="sm"
          className="h-7 gap-1.5 bg-allow text-xs text-white hover:bg-allow/90"
          onClick={() => onDecide(request.decision_id, true)}
        >
          <Check className="size-3" />
          Approve
        </Button>
      </div>
    </div>
  );
}
