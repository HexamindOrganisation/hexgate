import { useCallback, useEffect } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * Shared "which agent is the user looking at?" selection, backed by the
 * `?agent=` URL param so it survives navigation between /agents,
 * /policies (and any other future route that operates on one agent).
 *
 * Before this hook: each route held a local `useState` and auto-picked
 * the first agent on mount — so clicking "edit policy →" from /agents
 * would land on /policies with a DIFFERENT agent selected than the one
 * the user was just viewing. Wired through `?agent=`, the receiving
 * route reads the same value.
 *
 * Contract:
 *   * ``selected`` — the current agent name (from ?agent=), or ``null``
 *     if unset OR if the current value doesn't match any known agent
 *     (project switch, agent renamed/deleted, hand-typed URL).
 *   * ``set(name)`` — updates the URL param. Uses ``replace`` so the
 *     picker's onChange doesn't create a back-button trail of every
 *     click.
 *   * ``clear()`` — remove the param entirely (defers to auto-select).
 *
 * The auto-select-first fallback is preserved: when ``selected`` is
 * null AND ``knownNames`` is non-empty, callers still want to render
 * something; call ``set(knownNames[0])`` from an effect exactly the
 * way the local-state version used to.
 */
export interface AgentParam {
  selected: string | null;
  set: (name: string) => void;
  clear: () => void;
}

export function useAgentParam(
  knownNames: readonly string[] | undefined,
): AgentParam {
  const [params, setParams] = useSearchParams();
  const raw = params.get("agent");
  // Only expose a name if it actually matches something in the current
  // agent list — a URL like /policies?agent=stale won't leave the
  // picker in a broken "selected but not in the dropdown" state.
  const selected = raw && knownNames && knownNames.includes(raw) ? raw : null;

  const set = useCallback(
    (name: string) => {
      const next = new URLSearchParams(params);
      next.set("agent", name);
      setParams(next, { replace: true });
    },
    [params, setParams],
  );

  const clear = useCallback(() => {
    const next = new URLSearchParams(params);
    next.delete("agent");
    setParams(next, { replace: true });
  }, [params, setParams]);

  return { selected, set, clear };
}

/**
 * Auto-select-first bootstrap for routes that need SOMETHING rendered
 * on load rather than an empty state. Extracted so every consumer
 * doesn't re-write the same `if (!selected && knownNames.length) set(first)`
 * effect.
 *
 * Runs at most once per (knownNames identity) change; ``set`` is
 * stable via useCallback so it won't loop.
 */
export function useAutoSelectFirstAgent(
  agent: AgentParam,
  knownNames: readonly string[] | undefined,
): void {
  useEffect(() => {
    if (agent.selected === null && knownNames && knownNames.length > 0) {
      agent.set(knownNames[0]);
    }
  }, [agent, knownNames]);
}
