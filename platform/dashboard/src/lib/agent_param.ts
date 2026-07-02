import { useCallback, useEffect, useMemo, useRef } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * Shared "which agent is the user looking at?" selection, backed by the
 * `?agent=` URL param so it survives navigation between /agents,
 * /policies (and any other future route that operates on one agent).
 *
 * Before this hook: each route held a local ``useState`` and auto-picked
 * the first agent on mount — so clicking "edit policy →" from /agents
 * would land on /policies with a DIFFERENT agent selected than the one
 * the user was just viewing.
 *
 * ## Design constraints (learned the hard way in review)
 *
 * 1. **URL is source of truth, never silently rewritten.** If the user
 *    (or a shared bookmark) lands with ``?agent=support_bot`` and that
 *    agent was later renamed, we do NOT auto-rewrite the URL to some
 *    other agent — that turns "share this link" into "your colleague
 *    opens a random agent's policy." The stale name stays visible in
 *    the URL; ``fallbackName`` (the first agent, session-only) is what
 *    the picker/consumer actually renders while the URL disagrees.
 *
 * 2. **No mount-time clear.** A ``useEffect(clear, [projectId])`` fires
 *    on first render before the project ever "switches", so the effect
 *    would wipe every incoming ?agent= param on page load. Instead, the
 *    hook watches ``resetOn`` internally with a ref so it fires only on
 *    real transitions.
 *
 * 3. **Loading window preserves the URL.** During the query's first
 *    tick ``knownNames`` is an empty array or ``undefined``. Naively
 *    checking ``names.includes(raw)`` returns ``false`` and would drop
 *    the URL value on the floor. When names aren't known yet we trust
 *    the URL and expose ``raw`` — validation happens once the list
 *    arrives.
 *
 * 4. **Single hook, no split.** The two-hook split invited a caller to
 *    forget the memo or the auto-select effect; every caller ended up
 *    pasting the same three lines of glue. One hook, one call.
 */
export interface AgentSelection {
  /**
   * The agent name to hand to the picker + downstream data queries.
   *
   * Priority: ``?agent=`` if it matches a known name (or if names are
   * still loading) → first known name as a session-only fallback →
   * ``null``. The URL is never mutated by the fallback path — if you
   * see this value differ from the URL, it's a bookmarked-but-renamed
   * agent and the user should be aware.
   */
  selected: string | null;
  /** True when ``selected`` came from ``?agent=``, false when it's the
   *  first-agent fallback. Callers can surface an "agent not found in
   *  this project" banner instead of silently misrouting the user. */
  fromUrl: boolean;
  /** Called by the picker's onChange — updates ?agent= via ``replace``
   *  so picker clicks don't pollute the back-button trail. */
  set: (name: string) => void;
}

export interface UseAgentSelectionOptions {
  /**
   * Value whose change should reset the URL param (typically the
   * active project id). On the FIRST render the current value is
   * captured; the hook only clears when it later transitions to a
   * different value. Prevents the mount-clear bug that stomped every
   * incoming `?agent=` param on load.
   */
  resetOn?: string | null;
}

/**
 * The one-line agent-selection hook every route on the "picks one
 * agent" pattern should call.
 *
 * Pass the raw list you got from the API — the hook extracts names +
 * derives the selection + wires the URL. Callers write:
 *
 *   const { selected, set } = useAgentSelection(agents.data, {
 *     resetOn: scope.projectId,
 *   });
 *
 * Instead of the previous 3-line boilerplate + eslint-disable.
 */
export function useAgentSelection(
  agents: readonly { name: string }[] | undefined,
  options: UseAgentSelectionOptions = {},
): AgentSelection {
  const [params, setParams] = useSearchParams();
  const raw = params.get("agent");
  const namesLoaded = agents !== undefined;
  const knownNames = useMemo(
    () => (agents ? agents.map((a) => a.name) : []),
    [agents],
  );

  // Resolve `selected`. The three-way branch is deliberate:
  //   * names still loading → trust the URL (don't drop a valid value
  //     just because the list hasn't arrived yet).
  //   * URL points at a known name → use it.
  //   * URL is missing OR points at a name the list doesn't have →
  //     fall back to first known name (session-only, no URL write).
  let selected: string | null;
  let fromUrl: boolean;
  if (raw && !namesLoaded) {
    selected = raw;
    fromUrl = true;
  } else if (raw && knownNames.includes(raw)) {
    selected = raw;
    fromUrl = true;
  } else if (knownNames.length > 0) {
    selected = knownNames[0];
    fromUrl = false;
  } else {
    selected = null;
    fromUrl = false;
  }

  const set = useCallback(
    (name: string) => {
      const next = new URLSearchParams(params);
      next.set("agent", name);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  // Real transitions only — ref-tracked so the mount doesn't fire.
  // ``resetOn=undefined`` opts out of reset behavior entirely (useful
  // for routes with no project scope).
  const previousResetOn = useRef<string | null | undefined>(options.resetOn);
  useEffect(() => {
    if (previousResetOn.current === options.resetOn) return;
    previousResetOn.current = options.resetOn;
    // Only clear if the param is actually set — no-op writes still
    // touch history via useSearchParams, so we guard here.
    if (params.has("agent")) {
      const next = new URLSearchParams(params);
      next.delete("agent");
      setParams(next, { replace: true });
    }
  }, [options.resetOn, params, setParams]);

  return { selected, fromUrl, set };
}
