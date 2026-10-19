/**
 * Playground presentation-mode prefs, persisted per-device (localStorage) via
 * zustand `persist` — the same convention as theme.ts / ui.ts / active.ts — so a
 * custom accent and an acting name survive the "open in a new tab" flow and
 * reloads.
 *
 * The `accent` is global (it's a look). The acting `name` is keyed by project,
 * because it becomes the caller identity (→ `HexgateContext.user_id`) and must
 * NOT silently ride from one project's agent to another after a project switch.
 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

interface PlaygroundPrefs {
  /** Custom highlight as HSL channels ("H S% L%"); "" = the theme default. */
  accent: string;
  /** Acting name per project id (the demo caller identity). */
  names: Record<string, string>;
  setAccent: (accent: string) => void;
  setName: (projectId: string, name: string) => void;
}

export const usePlaygroundPrefs = create<PlaygroundPrefs>()(
  persist(
    (set) => ({
      accent: "",
      names: {},
      setAccent: (accent) => set({ accent }),
      setName: (projectId, name) =>
        set((s) => ({ names: { ...s.names, [projectId]: name } })),
    }),
    {
      name: "hexgate-playground",
      storage: createJSONStorage(() => localStorage),
    },
  ),
);
