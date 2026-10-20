import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Streamdown } from "streamdown";
import {
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  ChevronUp,
  Hexagon,
  RadioReceiver,
  ShieldAlert,
  SlidersHorizontal,
  SquarePen,
  Wrench,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  usePlayground,
  type ApprovalRequestEvent,
  type ChatMessage,
  type ToolCall,
} from "@/lib/playground";
import {
  AccentPicker,
  BAR_BTN,
  FullScreenToggle,
  ThemeToggle,
} from "@/components/playground/PresentationControls";
import { SessionSettings } from "@/components/playground/SessionSettings";
import { usePresentMode } from "@/lib/present";
import { DocsLink } from "@/components/DocsLink";
import { DOC_PATHS } from "@/lib/docs";
import { api, type AgentRead } from "@/lib/api";
import { useProjectScoped } from "@/lib/active";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { parseRolesFromPolicy } from "@/lib/policy";
import { useResolvedPolicy } from "@/lib/policy_files";
import {
  usePlaygroundAccent,
  usePlaygroundPrefs,
} from "@/lib/playground_prefs";
import { auditUrl } from "@/lib/audit-filters";
import { pluralise } from "@/lib/format";
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
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [agent, setAgent] = useState<AgentRead | null>(null);
  // A set, not a scalar: the enforcer evaluates every role the caller carries,
  // so the playground has to be able to reproduce a multi-role caller.
  const [activeRoles, setActiveRoles] = useState<string[]>([]);
  // Full-screen presentation mode (own tab, no app sidebar — see AppShell). The
  // playground drops its breakout margins and swaps "open in tab" for a close.
  // The acting name is subscribed inside the chip and the settings form rather
  // than here, so editing it doesn't re-render the whole transcript.
  const present = usePresentMode();
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

  // Whether the user was pinned to the bottom BEFORE the latest update — read
  // from the scroll handler, not measured post-render (a tall append or a
  // session mounted with history would misjudge it there). Starts true so a
  // fresh session lands on the newest turn.
  const atBottomRef = useRef(true);
  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = transcriptRef.current;
    el?.scrollTo({ top: el.scrollHeight });
  }, [state.messages]);

  // In presentation mode the serve process still holds the rehearsal history
  // from the tab this was opened from — `hexgate serve` keeps one chat history
  // per connection, so a fresh-looking full-screen tab would still have the
  // agent see those turns. Reset the session once on connect so the demo truly
  // starts clean.
  const didPresentReset = useRef(false);
  useEffect(() => {
    if (present && state.connected && !didPresentReset.current) {
      didPresentReset.current = true;
      reset();
    }
  }, [present, state.connected, reset]);

  function submit() {
    const text = composer.trim();
    if (!text) return;
    // Re-pin to the bottom so the sent turn and its streamed reply are visible
    // even if the user had scrolled up to re-read an earlier turn.
    atBottomRef.current = true;
    // Read the acting name imperatively (not a subscription) so typing it never
    // re-renders the transcript; sendChat trims it and omits an empty identity.
    const name = usePlaygroundPrefs.getState().names[projectId] ?? "";
    sendChat(text, { roles: activeRoles, name });
    setComposer("");
  }

  const empty = state.messages.length === 0;

  return (
    <AccentScope
      className={cn(
        "relative flex h-screen flex-col overflow-hidden bg-background",
        !present && "-mx-8 -my-6",
      )}
    >
      <GooFilter />

      <header className="flex items-center justify-between gap-3 px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          {present && (
            <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-action text-action-foreground">
              <Hexagon className="size-4" />
            </span>
          )}
          <AgentStatus
            agentName={state.agentName}
            online={state.agentOnline}
            relayConnected={state.connected}
          />
        </div>
        <div className="flex items-center gap-1">
          <DecisionSummary decisions={state.decisions} />
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            className={BAR_BTN}
          >
            <SlidersHorizontal className="size-4" />
            Settings
          </button>
          <AccentPicker />
          {present && <ThemeToggle />}
          <FullScreenToggle />
          {!present && (
            <button
              type="button"
              onClick={reset}
              disabled={empty}
              title="New chat"
              aria-label="New chat"
              className={BAR_BTN}
            >
              <SquarePen className="size-4" />
            </button>
          )}
        </div>
      </header>

      {/* Transcript */}
      <div
        ref={transcriptRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          atBottomRef.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        }}
        className="scrollbar-thin flex-1 overflow-y-auto"
      >
        <div className="mx-auto w-full max-w-3xl px-4 pb-6 pt-4">
          {empty ? (
            <EmptyState online={state.agentOnline} />
          ) : (
            <div className="space-y-6">
              {state.messages.map((m) => (
                <MessageView
                  key={m.id}
                  message={m}
                  agentName={state.agentName}
                  present={present}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Composer + inline approvals, centred like the transcript. */}
      <div className="relative z-10 bg-background">
        <div className="mx-auto w-full max-w-3xl px-4 pb-3">
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
          <Composer
            value={composer}
            onChange={setComposer}
            onSubmit={submit}
            agentName={state.agentName}
            identity={
              <IdentityChip
                projectId={projectId}
                activeRoles={activeRoles}
                onClick={() => setSettingsOpen(true)}
              />
            }
          />
          <div className="mt-2 flex items-center justify-center gap-3 text-xs text-muted-foreground">
            <span>
              Every tool call is checked against your policy and recorded in
              Audit.
            </span>
            <DocsLink path={DOC_PATHS.playground} label="Docs" compact />
          </div>
        </div>
      </div>

      <SessionSettings
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        projectId={projectId}
        roleOptions={roleOptions}
        activeRoles={activeRoles}
        onApply={setActiveRoles}
        onReset={reset}
        canReset={!empty}
      />
    </AccentScope>
  );
}

/** Owns the accent subscription so changing it repaints this wrapper, not the
 * transcript; its children are elements made by the parent. */
function AccentScope({
  className,
  children,
}: {
  className: string;
  children: React.ReactNode;
}) {
  const accent = usePlaygroundAccent();
  return (
    <div className={className} style={accent}>
      {children}
    </div>
  );
}

// ── Agent status ────────────────────────────────────────────────────────────

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
    "flex items-center gap-2 rounded-full border border-border px-3 py-1.5 text-xs";
  if (!agentName) {
    return (
      <div className={cn(pill, "text-muted-foreground")}>
        <RadioReceiver className="size-3.5 text-approval" />
        no agent serving
        {!relayConnected && (
          <span className="text-[10px] text-approval">· reconnecting…</span>
        )}
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
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-60" />
              <span className="relative inline-flex size-2 rounded-full bg-success" />
            </span>
            <span className="text-success">connected</span>
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

function roleLabel(activeRoles: string[]): string {
  if (activeRoles.length === 0) return "default";
  if (activeRoles.length === 1) return activeRoles[0];
  return `${activeRoles[0]} +${activeRoles.length - 1}`;
}

/** "{name} · {role}" in the composer; opens the session settings. Subscribes
 * to the acting name itself so typing it never re-renders the transcript. */
function IdentityChip({
  projectId,
  activeRoles,
  onClick,
}: {
  projectId: string;
  activeRoles: string[];
  onClick: () => void;
}) {
  const name = usePlaygroundPrefs((s) => s.names[projectId] ?? "").trim();
  return (
    <button
      type="button"
      onClick={onClick}
      title="Acting as — session settings"
      className="flex max-w-[60%] items-center gap-2 rounded-full border border-border px-2 py-1 text-xs transition-colors hover:bg-accent"
    >
      <span className="grid size-5 shrink-0 place-items-center rounded-full bg-primary/15 text-[10px] font-semibold uppercase text-primary">
        {(name || "p")[0]}
      </span>
      <span className="truncate">{name || "playground"}</span>
      <span className="text-muted-foreground">·</span>
      <span className="truncate font-mono">{roleLabel(activeRoles)}</span>
      <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
    </button>
  );
}

/** "N decisions · M failed" for the session, red when any failed. */
function DecisionSummary({ decisions }: { decisions: ToolCall[] }) {
  if (decisions.length === 0) return null;
  const failed = decisions.filter((d) => d.state === "failed").length;
  return (
    <span className="mr-2 hidden items-center gap-2 text-sm text-muted-foreground sm:flex">
      <span
        className={cn(
          "size-2 rounded-sm",
          failed ? "bg-deny" : "bg-muted-foreground",
        )}
      />
      {pluralise(decisions.length, "decision")}
      {failed > 0 && ` · ${failed} failed`}
    </span>
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
  agentName,
  identity,
}: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  agentName: string | null;
  identity: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-border bg-card p-3 shadow-sm transition-colors focus-within:border-primary/60">
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          // Enter confirming an IME candidate isn't a send.
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            onSubmit();
          }
        }}
        rows={2}
        placeholder={`Message ${agentName ?? "the agent"}…`}
        aria-label="Message the agent"
        className="block max-h-48 w-full resize-none bg-transparent px-1 text-sm focus:outline-none [field-sizing:content]"
      />
      <div className="mt-2 flex items-center justify-between gap-2">
        {identity}
        <button
          type="button"
          onClick={onSubmit}
          disabled={!value.trim()}
          aria-label="Send"
          className="grid size-9 place-items-center rounded-full bg-action text-action-foreground transition-all hover:bg-action/85 disabled:scale-90 disabled:bg-muted disabled:text-muted-foreground"
        >
          <ArrowUp className="size-4" />
        </button>
      </div>
    </div>
  );
}

// ── Transcript ──────────────────────────────────────────────────────────────

function MessageView({
  message,
  agentName,
  present,
}: {
  message: ChatMessage;
  agentName: string | null;
  present: boolean;
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
  const hasSteps = !!turn && (turn.reasoning !== "" || turn.tools.length > 0);
  return (
    <div className="pg-enter flex items-start gap-3">
      <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-action text-action-foreground">
        <Hexagon className="size-3.5" />
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
              <DecisionCard key={t.id} call={t} agentName={agentName} />
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
  // One announcement: the visible label carries the status; the goo is decorative.
  return (
    <div className="flex flex-col gap-1.5" role="status">
      <span className="pg-shimmer text-xs font-medium">Thinking…</span>
      <div className="pg-goo" aria-hidden>
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

const DECISION: Record<
  ToolCall["state"],
  { label: string; dot: string; text: string; border: string }
> = {
  completed: {
    label: "Allowed",
    dot: "bg-muted-foreground",
    text: "text-foreground",
    border: "border-border",
  },
  failed: {
    label: "Failed",
    dot: "bg-deny",
    text: "text-deny",
    border: "border-deny/50",
  },
  started: {
    label: "Running",
    dot: "bg-approval",
    text: "text-approval",
    border: "border-approval/50",
  },
};

/** `name(k=v, …)`: the call as the agent made it. */
function callSignature(call: ToolCall): string {
  const args = Object.entries(call.args)
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(", ");
  return `(${args})`;
}

/** One tool call inline in the turn, collapsible; a failed call opens by
 * default so the reason is in view. */
function DecisionCard({
  call,
  agentName,
}: {
  call: ToolCall;
  agentName: string | null;
}) {
  const [open, setOpen] = useState<boolean | null>(null);
  const shown = open ?? call.state === "failed";
  const d = DECISION[call.state];
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
          <span className="text-muted-foreground">{callSignature(call)}</span>
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
              (call.state === "started"
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
          {agentName && (
            <Link
              to={auditUrl({ agent: agentName, tool: call.name, range: "24h" })}
              className="inline-block font-medium text-primary hover:underline"
            >
              Open in audit
            </Link>
          )}
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
      {/* Cap the pile so N concurrent approvals can't grow past the viewport and
          push the composer (and lower Approve/Deny buttons) out of reach — the
          header stays pinned, the cards scroll. */}
      <div className="max-h-[45vh] space-y-2 overflow-y-auto scrollbar-thin">
        {pending.map((req) => (
          <ApprovalPromptCard
            key={req.decision_id}
            request={req}
            onDecide={onDecide}
            now={now}
          />
        ))}
      </div>
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
          className="h-7 gap-1.5 text-xs"
          onClick={() => onDecide(request.decision_id, true)}
        >
          <Check className="size-3" />
          Approve
        </Button>
      </div>
    </div>
  );
}
