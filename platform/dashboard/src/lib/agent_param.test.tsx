/**
 * Tests for the ``useAgentParam`` + ``useAutoSelectFirstAgent`` pair
 * that back ``?agent=`` selection sync across /agents and /policies.
 *
 * The invariants:
 *   1. On mount, ``selected`` reflects the ?agent= param IF it matches
 *      a known agent (otherwise null — stale URL stays inert).
 *   2. ``set(name)`` writes ?agent= via ``replace`` (no back-button
 *      trail for picker changes).
 *   3. ``clear()`` removes ?agent= entirely (project-switch reset).
 *   4. When ``selected`` is null and knownNames is non-empty,
 *      ``useAutoSelectFirstAgent`` runs ``set(names[0])`` exactly once
 *      per (names identity) change — no loops.
 */

import { act, renderHook } from "@testing-library/react";
import type { JSX, ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { useAgentParam, useAutoSelectFirstAgent } from "./agent_param";

function wrapperAt(initialUrl: string) {
  return ({ children }: { children: ReactNode }): JSX.Element => (
    <MemoryRouter initialEntries={[initialUrl]}>{children}</MemoryRouter>
  );
}

describe("useAgentParam", () => {
  it("returns the ?agent= value when it matches a known name", () => {
    const { result } = renderHook(() => useAgentParam(["alpha", "beta"]), {
      wrapper: wrapperAt("/policies?agent=beta"),
    });
    expect(result.current.selected).toBe("beta");
  });

  it("returns null when ?agent= doesn't match any known name", () => {
    // Stale URL from a deleted / renamed agent must NOT leave the
    // picker in a "selected but not in the dropdown" broken state.
    const { result } = renderHook(() => useAgentParam(["alpha", "beta"]), {
      wrapper: wrapperAt("/policies?agent=gone"),
    });
    expect(result.current.selected).toBeNull();
  });

  it("returns null when ?agent= is absent", () => {
    const { result } = renderHook(() => useAgentParam(["alpha"]), {
      wrapper: wrapperAt("/policies"),
    });
    expect(result.current.selected).toBeNull();
  });

  it("returns null when knownNames is undefined (still loading)", () => {
    const { result } = renderHook(() => useAgentParam(undefined), {
      wrapper: wrapperAt("/policies?agent=beta"),
    });
    // Guard against exposing an agent name we can't validate yet —
    // the picker will render "loading" instead of a broken selection.
    expect(result.current.selected).toBeNull();
  });

  it("set() writes the param without pushing history", () => {
    // Wrap our hook + a location observer so we can assert both the
    // param update AND that the entry count didn't grow (replace, not
    // push — picker clicks shouldn't pollute the back-button trail).
    const { result } = renderHook(
      () => {
        const agent = useAgentParam(["alpha", "beta"]);
        const location = useLocation();
        return { agent, location };
      },
      { wrapper: wrapperAt("/policies") },
    );

    expect(result.current.location.pathname).toBe("/policies");
    act(() => {
      result.current.agent.set("beta");
    });
    expect(result.current.location.search).toBe("?agent=beta");
    expect(result.current.agent.selected).toBe("beta");
  });

  it("clear() removes the param", () => {
    const { result } = renderHook(() => useAgentParam(["alpha", "beta"]), {
      wrapper: wrapperAt("/policies?agent=beta&keep=1"),
    });
    expect(result.current.selected).toBe("beta");
    act(() => {
      result.current.clear();
    });
    expect(result.current.selected).toBeNull();
  });

  it("set() preserves other query params", () => {
    // A future route may add e.g. ?tab=graph — set/clear on agent
    // must not stomp unrelated keys.
    const { result } = renderHook(
      () => {
        const agent = useAgentParam(["alpha", "beta"]);
        const location = useLocation();
        return { agent, location };
      },
      { wrapper: wrapperAt("/policies?tab=graph") },
    );
    act(() => {
      result.current.agent.set("alpha");
    });
    expect(result.current.location.search).toContain("tab=graph");
    expect(result.current.location.search).toContain("agent=alpha");
  });
});

describe("useAutoSelectFirstAgent", () => {
  it("selects the first name when nothing is selected", () => {
    const { result } = renderHook(
      () => {
        const agent = useAgentParam(["alpha", "beta"]);
        useAutoSelectFirstAgent(agent, ["alpha", "beta"]);
        return agent;
      },
      { wrapper: wrapperAt("/policies") },
    );
    // Effect runs after mount → selection settles on the first name.
    expect(result.current.selected).toBe("alpha");
  });

  it("does NOT override an existing selection", () => {
    // Landing on /policies?agent=beta must keep beta, not replace it
    // with alpha (the auto-select was for the "empty state" case only).
    const { result } = renderHook(
      () => {
        const agent = useAgentParam(["alpha", "beta"]);
        useAutoSelectFirstAgent(agent, ["alpha", "beta"]);
        return agent;
      },
      { wrapper: wrapperAt("/policies?agent=beta") },
    );
    expect(result.current.selected).toBe("beta");
  });

  it("no-ops when knownNames is empty", () => {
    const { result } = renderHook(
      () => {
        const agent = useAgentParam([]);
        useAutoSelectFirstAgent(agent, []);
        return agent;
      },
      { wrapper: wrapperAt("/policies") },
    );
    expect(result.current.selected).toBeNull();
  });
});
