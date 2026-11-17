import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { Filter, Lightbulb, X } from "lucide-react";
import {
  api,
  type AuditDecisionRow,
  type AuditOutcome,
  LLM_MESSAGE_PAGE,
} from "@/lib/api";
import { type SetAuditFilters } from "@/lib/audit-filters";
import { Button } from "@/components/ui/button";
import { DecisionBadge } from "@/components/audit/charts";
import { OUT_LABEL } from "@/components/audit/chart-tokens";
import { fmtTs } from "@/components/audit/fmt";
import { LlmMessagesSection } from "@/components/audit/messages";

const fmtFull = (d: Date) =>
  d.toISOString().replace("T", " ").replace("Z", " UTC");

function KV({
  k,
  children,
  mono,
  muted,
}: {
  k: string;
  children: React.ReactNode;
  mono?: boolean;
  muted?: boolean;
}) {
  return (
    <div className="grid grid-cols-[116px_1fr] items-baseline gap-2.5 py-[5px] text-meta">
      <div className="text-muted-foreground">{k}</div>
      <div
        className={`break-words ${mono ? "font-mono text-xs" : ""} ${muted ? "text-muted-foreground" : "text-foreground"}`}
      >
        {children}
      </div>
    </div>
  );
}
function DrawerSection({
  label,
  children,
  accent,
}: {
  label: string;
  children: React.ReactNode;
  accent?: string;
}) {
  return (
    <div className="mt-[22px]">
      <div
        className={`mb-2 text-[11px] font-medium uppercase tracking-wider ${accent || "text-muted-foreground"}`}
      >
        {label}
      </div>
      {children}
    </div>
  );
}

// Per-outcome drawer accents (reason box + error text).
const TONE: Record<AuditOutcome, { text: string; box: string }> = {
  allow: { text: "text-allow", box: "border-allow/25 bg-allow/10" },
  deny: { text: "text-deny", box: "border-deny/25 bg-deny/10" },
  needs_approval: {
    text: "text-approval",
    box: "border-approval/25 bg-approval/10",
  },
};

export function DetailDrawer({
  event,
  related,
  projectId,
  onClose,
  onSelect,
  setF,
}: {
  event: AuditDecisionRow | null;
  related: AuditDecisionRow[];
  projectId: string | null;
  onClose: () => void;
  onSelect: (e: AuditDecisionRow) => void;
  /** The "Filter to…" footer; omit it (e.g. the agent hub) to hide the footer,
   * since those actions only make sense against the full audit filter bar. */
  setF?: SetAuditFilters;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Both scopes go out exactly as the row holds them, blanks included — see
  // api.listLlmMessages. Two blanks name no transcript, so the query is only
  // enabled when at least one is set.
  const scoped = !!(event?.session_id || event?.run_id);
  // Keyed on the decision, not just the session: a page is a window on the
  // transcript and which window we want depends on when the decision
  // happened, so two decisions far apart in one long session need two
  // fetches. Short transcripts — the overwhelming majority — come back whole
  // on the first request and every decision in them reuses that one entry.
  const messagesQ = useQuery({
    queryKey: [
      "audit",
      "llm-messages",
      projectId,
      event?.session_id,
      event?.run_id,
      event?.occurred_at,
    ],
    enabled: !!projectId && scoped,
    // One attempt, against the default of three. Every other read here is a
    // kilobyte of decision rows; this one can page over a transcript of
    // ~272 KiB rows with an uncapped offset, and ClickHouse runs it against
    // a server-wide memory budget shared with the enricher's inserts. Three
    // silent retries turn one expensive read into four, on a section the
    // rest of the drawer does not depend on.
    retry: 1,
    queryFn: async () => {
      const args = [
        projectId as string,
        event!.session_id,
        event!.run_id,
      ] as const;
      const head = await api.listLlmMessages(...args);
      // Rows are oldest-first, so a truncated page is the transcript's HEAD
      // and the turns after it are the ones missing. When the decision is
      // later than every row we got, its anchor is in that missing tail —
      // keeping the head would silently highlight some earlier call as "this
      // call". Take the last window instead, which is where a decision late
      // in a long conversation sits.
      const last = head.rows[head.rows.length - 1];
      if (
        head.total > head.rows.length &&
        last &&
        new Date(last.occurred_at) <= new Date(event!.occurred_at)
      ) {
        return api.listLlmMessages(
          ...args,
          LLM_MESSAGE_PAGE,
          head.total - LLM_MESSAGE_PAGE,
        );
      }
      return head;
    },
  });
  if (!event) return null;
  const e = event;
  const argStr =
    e.arguments == null ? "—" : JSON.stringify(e.arguments, null, 2);
  const attrStr =
    e.attributes == null ? null : JSON.stringify(e.attributes, null, 2);
  const hintStr =
    typeof e.hint === "string"
      ? e.hint
      : e.hint == null
        ? ""
        : JSON.stringify(e.hint);
  const tone = TONE[e.outcome];

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div
        onClick={onClose}
        className="absolute inset-0 bg-black/55 backdrop-blur-[1px]"
      />
      <aside className="relative flex h-full w-[472px] max-w-[92vw] flex-col border-l border-border bg-card shadow-2xl">
        <div className="flex items-center gap-2.5 border-b border-border px-5 py-4">
          <DecisionBadge d={e.outcome} />
          <span className="flex-1 truncate font-mono text-xs text-muted-foreground">
            {e.event_id}
          </span>
          <Button
            variant="ghost"
            size="icon"
            onClick={onClose}
            title="Close (Esc)"
          >
            <X className="size-4" />
          </Button>
        </div>

        <div className="flex-1 overflow-auto px-5 pb-6 pt-1 scrollbar-thin">
          <div className="mt-[18px] flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-[17px] font-semibold text-foreground">
              {e.tool_name}
            </span>
            <span className="text-[13px] text-muted-foreground">by</span>
            <span className="font-mono text-sm text-foreground">
              {e.agent_name}
            </span>
          </div>
          <div
            className={`mt-2 rounded-lg border px-3 py-2.5 text-[13px] leading-relaxed text-foreground ${tone.box}`}
          >
            {e.reason ||
              (e.outcome === "allow"
                ? "Allowed — no rule violated."
                : "No reason recorded.")}
            {e.error_type && (
              <span
                className={`mt-1.5 block font-mono text-caption ${tone.text}`}
              >
                error_type: {e.error_type}
              </span>
            )}
          </div>

          {e.violations.length > 0 && (
            <DrawerSection label="Violations" accent="text-deny">
              <div className="flex flex-wrap gap-1.5">
                {e.violations.map((v) => (
                  <span
                    key={v}
                    className="rounded-md bg-deny/15 px-2 py-[3px] font-mono text-caption text-deny"
                  >
                    {v}
                  </span>
                ))}
              </div>
            </DrawerSection>
          )}

          {hintStr && (
            <DrawerSection label="Policy hint">
              <div className="flex items-center gap-2 rounded-md border border-border bg-muted px-3 py-2.5 font-mono text-xs">
                <Lightbulb className="size-3.5 shrink-0 text-approval" />
                <span className="text-foreground">{hintStr}</span>
              </div>
            </DrawerSection>
          )}

          <DrawerSection label="Decision">
            <KV k="outcome">{OUT_LABEL[e.outcome]}</KV>
            <KV k="tool_name" mono>
              {e.tool_name}
            </KV>
            {/* Two rows, not three: the legacy scalar `role` would say almost
                the same thing as "roles evaluated". */}
            <KV k="roles evaluated">
              {e.user_roles.length ? (
                e.user_roles.join(", ")
              ) : (
                <span className="text-muted-foreground">∅ none</span>
              )}
            </KV>
            {/* An empty deciding_role means three different things on the wire:
                every role denied, no roles evaluated at all (the enforcer ran
                without a HexgateContext, so `default` decided), or a pre-multi-
                role SDK that only sent the scalar. outcome + user_roles tell
                them apart — without this branch an allow reads as a denial. */}
            <KV k="granted by">
              {e.deciding_role || (
                <span className="text-muted-foreground">
                  {e.outcome === "deny"
                    ? "∅ none — no role granted it"
                    : e.user_roles.length === 0
                      ? "default policy — no roles evaluated"
                      : "not recorded (legacy event)"}
                </span>
              )}
            </KV>
            {e.error_type && (
              <KV k="error_type" mono>
                {e.error_type}
              </KV>
            )}
          </DrawerSection>

          <DrawerSection label="Arguments">
            <pre className="m-0 whitespace-pre-wrap rounded-lg border border-border bg-muted p-3 font-mono text-caption leading-relaxed text-foreground">
              {argStr}
            </pre>
          </DrawerSection>

          <DrawerSection label="Messages">
            <LlmMessagesSection
              rows={messagesQ.data?.rows ?? []}
              total={messagesQ.data?.total ?? 0}
              decisionOccurredAt={e.occurred_at}
              offset={messagesQ.data?.offset ?? 0}
              scoped={scoped}
              // The same condition as `enabled`, not a subset of it: a
              // disabled query stays `pending` forever, so a clause missing
              // here would show "Loading…" with no request in flight.
              isLoading={messagesQ.isPending && !!projectId && scoped}
              isError={messagesQ.isError}
            />
          </DrawerSection>

          {attrStr && (
            <DrawerSection label="Context attributes">
              <pre className="m-0 whitespace-pre-wrap rounded-lg border border-border bg-muted p-3 font-mono text-caption leading-relaxed text-foreground">
                {attrStr}
              </pre>
            </DrawerSection>
          )}

          <DrawerSection label="Envelope">
            <KV k="occurred_at" mono>
              {fmtFull(new Date(e.occurred_at))}
            </KV>
            <KV k="received_at" mono muted>
              {fmtFull(new Date(e.received_at))}
            </KV>
            <KV k="agent_name" mono>
              {e.agent_name}
            </KV>
            <KV k="session_id" mono muted>
              {e.session_id || "∅"}
            </KV>
            <KV k="user_id" mono muted>
              {e.user_id || "∅"}
            </KV>
          </DrawerSection>

          <DrawerSection label={`Same session · ${related.length}`}>
            {related.length ? (
              <div className="overflow-hidden rounded-lg border border-border">
                {related.map((r, i) => (
                  <div
                    key={r.event_id}
                    onClick={() => onSelect(r)}
                    className={`flex cursor-pointer items-center gap-2 px-2.5 py-2 text-xs hover:bg-accent ${i < related.length - 1 ? "border-b border-border" : ""}`}
                  >
                    <DecisionBadge d={r.outcome} />
                    <span className="flex-1 truncate font-mono">
                      {r.tool_name}
                    </span>
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {fmtTs(new Date(r.occurred_at))}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-muted-foreground">
                No other events in this session.
              </div>
            )}
          </DrawerSection>
        </div>

        {setF && (
          <div className="flex gap-2 border-t border-border px-5 py-3">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setF((p) => ({ ...p, agent: e.agent_name }));
                onClose();
              }}
            >
              <Filter className="size-3" />
              Filter to agent
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setF((p) => ({ ...p, tool: e.tool_name }));
                onClose();
              }}
            >
              <Filter className="size-3" />
              Filter to tool
            </Button>
          </div>
        )}
      </aside>
    </div>
  );
}
