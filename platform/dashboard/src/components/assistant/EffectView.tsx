import { ArrowRight } from "lucide-react";

import type {
  PolicyAgentChange,
  PolicyChange,
  PolicyDiff,
  PolicyGateChange,
  PolicyGrant,
} from "@/lib/api";

const MODE_LABEL: Record<string, string> = {
  allow: "allowed",
  approval_required: "needs approval",
};

/** What a change does to the policy: who gains or loses what, then the
 * agent-level and gate changes. `effect` is read from a stored row, so any
 * list may be missing. */
export function EffectView({ effect }: { effect: PolicyDiff }) {
  const changes = effect.changes ?? [];
  const agentChanges = effect.agent_changes ?? [];
  const gateChanges = effect.gate_changes ?? [];
  // Nothing was compared when a side does not compose: the note says so, and
  // "nothing changes" would contradict it.
  const compared =
    !effect.composes || (effect.composes.before && effect.composes.after);
  const empty =
    compared && changes.length + agentChanges.length + gateChanges.length === 0;
  return (
    <div className="space-y-3 px-3 py-2">
      <ComposesNote composes={effect.composes} />
      {empty && (
        <p className="text-muted-foreground">
          No agent gains or loses anything.
        </p>
      )}
      {changes.length > 0 && (
        <ul aria-label="Grant changes" className="space-y-1.5">
          {changes.map((c) => (
            <GrantRow key={`${c.agent}|${c.role}|${c.tool}`} change={c} />
          ))}
        </ul>
      )}
      {agentChanges.length > 0 && (
        <ul aria-label="Agent changes" className="space-y-1.5">
          {agentChanges.map((c) => (
            <AgentRow key={`${c.agent}|${c.key}`} change={c} />
          ))}
        </ul>
      )}
      {gateChanges.length > 0 && (
        <ul aria-label="Gate changes" className="space-y-1.5">
          {gateChanges.map((c) => (
            <GateRow key={`${c.agent}|${c.gate}`} change={c} />
          ))}
        </ul>
      )}
    </div>
  );
}

function ComposesNote({ composes }: { composes: PolicyDiff["composes"] }) {
  if (!composes || (composes.before && composes.after)) return null;
  const text = composes.before
    ? "The policy does not compose after this change."
    : composes.after
      ? "The policy did not compose before this change."
      : "The policy composes neither before nor after this change.";
  return <p className="text-muted-foreground">{text}</p>;
}

function GrantRow({ change }: { change: PolicyChange }) {
  const added = change.constraints_added ?? [];
  const removed = change.constraints_removed ?? [];
  // The mode both sides share, when only the constraints moved.
  const keptMode =
    change.before && change.after && change.before.mode === change.after.mode
      ? change.after.mode
      : null;
  return (
    <li>
      <p className="flex flex-wrap items-center gap-x-1.5">
        <span className="font-mono">
          {change.agent} · {change.role} · {change.tool}
        </span>
        {keptMode !== null ? (
          <span>
            {MODE_LABEL[keptMode] ?? keptMode},{" "}
            {added.length + removed.length === 0
              ? "constraints reordered"
              : "constraints changed"}
          </span>
        ) : (
          <>
            <Grant grant={change.before} />
            <ArrowRight aria-label="becomes" className="size-3 shrink-0" />
            <Grant grant={change.after} />
          </>
        )}
      </p>
      {added.map((c) => (
        <p key={`+${c}`} className="font-mono text-allow">
          + {c}
        </p>
      ))}
      {removed.map((c) => (
        <p key={`-${c}`} className="font-mono text-deny">
          − {c}
        </p>
      ))}
    </li>
  );
}

function Grant({ grant }: { grant: PolicyGrant | null }) {
  if (!grant) return <span className="text-muted-foreground">no access</span>;
  return <span>{MODE_LABEL[grant.mode] ?? grant.mode}</span>;
}

function AgentRow({ change }: { change: PolicyAgentChange }) {
  return (
    <li>
      <p className="font-mono">
        {change.agent} · {change.key}
      </p>
      {change.before !== null && (
        <p className="break-all font-mono text-deny">
          − {JSON.stringify(change.before)}
        </p>
      )}
      {change.after !== null && (
        <p className="break-all font-mono text-allow">
          + {JSON.stringify(change.after)}
        </p>
      )}
    </li>
  );
}

/** What switching a gate does, per gate: admission and reach, once off, let
 * every caller through; the as-tool and skill gates, once off, fall back to
 * the tool's own rule. Gate ids are the keys of `GATES` in the api's
 * policy_modules/diff.py. */
const GATE_EFFECT: Record<string, { on: string; off: string; opens: boolean }> =
  {
    admission: {
      on: "now restricted to the roles that list it",
      off: "now open to every caller",
      opens: true,
    },
    reach: {
      on: "now restricted to the roles that list it",
      off: "now open to every caller",
      opens: true,
    },
    tool_reach: {
      on: "now governed by the roles that list it",
      off: "falls back to the tool's own rule",
      opens: false,
    },
    skills: {
      on: "now governed by the roles that list it",
      off: "falls back to the tool's own rule",
      opens: false,
    },
  };

function GateRow({ change }: { change: PolicyGateChange }) {
  const effect = GATE_EFFECT[change.gate];
  // Only a gate that opens to every caller when off is a loosening to flag.
  const tone = effect?.opens && !change.after ? "text-deny" : "";
  return (
    <li>
      <span className="font-mono">
        {change.agent} · {change.gate}
      </span>{" "}
      {change.after ? "switched on" : "switched off"}
      {effect && (
        <>
          :{" "}
          <span className={tone}>{change.after ? effect.on : effect.off}</span>
        </>
      )}
    </li>
  );
}
