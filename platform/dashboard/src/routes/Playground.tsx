import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Bot,
  Check,
  ChevronDown,
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
  callerName,
  usePlayground,
  type ApprovalRequestEvent,
  type ToolCall,
} from "@/lib/playground";
import { BAR_BTN } from "@/components/playground/bar-btn";
import {
  AccentPicker,
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
import { pluralise } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Composer } from "@/components/chat/Composer";
import { GooFilter } from "@/components/chat/ThinkingGoo";
import { MessageView } from "@/components/chat/MessageView";
import {
  callKey,
  newTabWhen,
  verdictOf,
  type Verdict,
} from "@/components/chat/verdict";

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

export function PlaygroundLive({
  projectId,
  embedded = false,
}: {
  projectId: string;
  /** Rendered inside a hub tab (bounded panel) rather than full-screen. */
  embedded?: boolean;
}) {
  const { state, sendChat, reset, respondToApproval } = usePlayground({
    projectId,
  });
  const [settingsOpen, setSettingsOpen] = useState(false);
  // Bumped on each opening, so the settings draft starts from what's applied.
  const [settingsSession, setSettingsSession] = useState(0);
  const openSettings = () => {
    setSettingsSession((n) => n + 1);
    setSettingsOpen(true);
  };
  // Tagged with the project and name it was fetched for, so a switch never
  // reads the previous agent's roles while the new one loads.
  const [fetched, setFetched] = useState<{
    key: string;
    agent: AgentRead | null;
  } | null>(null);
  const agentKey = `${projectId}\u0000${state.agentName ?? ""}`;
  const agent = fetched?.key === agentKey ? fetched.agent : null;
  // The roles the user applied in this project, saved with the acting name:
  // unset until they choose (the agent's first role then), [] for "no role".
  // A set, not a scalar: the enforcer evaluates every role the caller carries.
  const saved = usePlaygroundPrefs((s) => s.roles[projectId]);
  const setRoles = usePlaygroundPrefs((s) => s.setRoles);
  // Full-screen presentation mode (own tab, no app sidebar — see AppShell): no
  // breakout margins, "Exit full screen", and a theme toggle for New chat. The
  // acting name is subscribed inside the chip and the settings form rather
  // than here, so editing it doesn't re-render the whole transcript.
  const present = usePresentMode();
  const transcriptRef = useRef<HTMLDivElement>(null);

  // Fetch the serving agent so we know which roles are available. Roles are a
  // per-agent concept today (M1); a later global role registry moves this hook.
  useEffect(() => {
    if (!state.agentName) return;
    let cancelled = false;
    api
      .getAgent(state.agentName, projectId)
      .then((a) => {
        if (!cancelled) setFetched({ key: agentKey, agent: a });
      })
      .catch(() => {
        if (!cancelled) setFetched({ key: agentKey, agent: null });
      });
    return () => {
      cancelled = true;
    };
  }, [state.agentName, projectId, agentKey]);

  // Roles for the session settings. A compose (modular) project has no
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

  // The roles this turn carries, derived (not synced) so a choice outlives an
  // agent that briefly has no roles: the chosen roles the agent still
  // defines, else its first role, unless "no role". While serve is down a
  // queued turn keeps the choice; an online agent with no roles gets none.
  // A choice applies to the agent it was made for, not whatever serves next;
  // while none is serving (a restart clears the name) it still applies. One
  // made before any agent served (agent "") goes to the first that connects.
  const choice =
    saved &&
    (!state.agentName || saved.agent === "" || saved.agent === state.agentName)
      ? saved.roles
      : undefined;
  useEffect(() => {
    if (state.agentName && saved?.agent === "")
      setRoles(projectId, state.agentName, saved.roles);
  }, [state.agentName, saved, setRoles, projectId]);
  const agentOnline = state.agentOnline;
  const activeRoles = useMemo(() => {
    if (roleOptions.length === 0) return agentOnline ? [] : (choice ?? []);
    if (choice?.length === 0) return [];
    const kept = (choice ?? []).filter((r) => roleOptions.includes(r));
    return kept.length ? kept : [roleOptions[0]];
  }, [choice, roleOptions, agentOnline]);
  // null: the roles weren't touched in the dialog, so the choice stands.
  const applyRoles = (roles: string[] | null) => {
    // With no agent serving (a restart), keep the choice's agent; before any
    // agent has served, "" stands for the next one to connect.
    const agent = state.agentName ?? saved?.agent ?? "";
    if (roles !== null) setRoles(projectId, agent, roles);
  };

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

  function submit(text: string) {
    // Re-pin to the bottom so the sent turn and its streamed reply are visible
    // even if the user had scrolled up to re-read an earlier turn.
    atBottomRef.current = true;
    // Read the acting name imperatively (not a subscription) so typing it never
    // re-renders the transcript; sendChat trims it and omits an empty identity.
    const name = usePlaygroundPrefs.getState().names[projectId] ?? "";
    sendChat(text, { roles: activeRoles, name });
  }

  const empty = state.messages.length === 0;
  // Calls held at the approval prompt (tool + arguments): their cards say
  // so, not "Running".
  const awaiting = useMemo(
    () =>
      new Set(
        state.pendingApprovals.map((a) => callKey(a.tool_name, a.arguments)),
      ),
    [state.pendingApprovals],
  );

  return (
    <AccentScope
      className={cn(
        "relative flex flex-col overflow-hidden bg-background",
        // Embedded in a hub tab: a bounded, bordered panel (shrinks on short
        // viewports). Standalone: the full-screen surface that breaks out of
        // the page padding. The --pg-* vars cap the approval pile and empty
        // state relative to whichever surface this is, so a tall viewport's
        // 45vh can't overflow the ~640px panel and clip the composer.
        embedded
          ? "h-[min(640px,72vh)] rounded-xl border border-border [--pg-approvals-cap:min(14rem,30vh)] [--pg-empty-min:12rem]"
          : "h-screen [--pg-approvals-cap:45vh] [--pg-empty-min:50vh]",
        !present && !embedded && "-mx-8 -my-6",
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
            present={present}
          />
        </div>
        <div className="flex items-center gap-1">
          <DecisionSummary decisions={state.decisions} />
          <button type="button" onClick={openSettings} className={BAR_BTN}>
            <SlidersHorizontal className="size-4" />
            Settings
          </button>
          {!embedded && <AccentPicker />}
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
                  awaiting={awaiting}
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
            onSubmit={submit}
            agentName={state.agentName}
            identity={
              <IdentityChip
                projectId={projectId}
                activeRoles={activeRoles}
                onClick={openSettings}
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
        session={settingsSession}
        agentName={state.agentName}
        projectId={projectId}
        roleOptions={roleOptions}
        activeRoles={activeRoles}
        onApply={applyRoles}
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
  present,
}: {
  agentName: string | null;
  online: boolean;
  relayConnected: boolean;
  present: boolean;
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
      {...newTabWhen(present)}
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
  const name = usePlaygroundPrefs((s) => s.names[projectId] ?? "");
  const caller = callerName(name, activeRoles) ?? "default caller";
  return (
    <button
      type="button"
      onClick={onClick}
      title="Acting as — session settings"
      className="flex max-w-[60%] items-center gap-2 rounded-full border border-border px-2 py-1 text-xs transition-colors hover:bg-accent"
    >
      <span className="grid size-5 shrink-0 place-items-center rounded-full bg-primary/15 text-[10px] font-semibold uppercase text-primary">
        {caller[0]}
      </span>
      <span className="truncate">{caller}</span>
      {activeRoles.length > 0 && (
        <>
          <span className="text-muted-foreground">·</span>
          <span className="truncate font-mono">{roleLabel(activeRoles)}</span>
        </>
      )}
      <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
    </button>
  );
}

/** "N decisions · M denied · K not approved · J failed" for the session, red
 * when any call was stopped or failed. Memoised: the page re-renders per
 * streamed token. */
const DecisionSummary = memo(function DecisionSummary({
  decisions,
}: {
  decisions: ToolCall[];
}) {
  if (decisions.length === 0) return null;
  const verdicts = decisions.map((d) => verdictOf(d, false));
  const count = (v: Verdict) => verdicts.filter((x) => x === v).length;
  const [denied, held, failed] = [
    count("denied"),
    count("held"),
    count("failed"),
  ];
  return (
    <span className="mr-2 hidden items-center gap-2 text-sm text-muted-foreground sm:flex">
      <span
        className={cn(
          "size-2 rounded-sm",
          denied || failed
            ? "bg-deny"
            : held
              ? "bg-approval"
              : "bg-muted-foreground",
        )}
      />
      {pluralise(decisions.length, "decision")}
      {denied > 0 && ` · ${denied} denied`}
      {held > 0 && ` · ${held} not approved`}
      {failed > 0 && ` · ${failed} failed`}
    </span>
  );
});

function EmptyState({ online }: { online: boolean }) {
  return (
    <div className="grid min-h-[var(--pg-empty-min,50vh)] place-items-center text-center">
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
      <div className="max-h-[var(--pg-approvals-cap,45vh)] space-y-2 overflow-y-auto scrollbar-thin">
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
          // Neutral, never the Playground accent; green means "connected".
          className="h-7 gap-1.5 bg-foreground text-xs text-background hover:bg-foreground/85"
          onClick={() => onDecide(request.decision_id, true)}
        >
          <Check className="size-3" />
          Approve
        </Button>
      </div>
    </div>
  );
}
