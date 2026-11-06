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

import { SCHEME_ACCENTS } from "@/lib/theme";

interface PlaygroundPrefs {
  /** Custom highlight as HSL channels ("H S% L%"); "" = the theme default. */
  accent: string;
  /** Acting name per project id (the demo caller identity). */
  names: Record<string, string>;
  setAccent: (accent: string) => void;
  setName: (projectId: string, name: string) => void;
}

/** v0 preset value → its v1 equivalent (old dark blue; old fixed violet → dark, the default mode). */
const LEGACY_ACCENTS: Record<string, string> = {
  "226 78% 65%": SCHEME_ACCENTS.blue.dark,
  "262 70% 58%": SCHEME_ACCENTS.violet.dark,
};

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
      version: 1,
      migrate: (persisted, version) => {
        const s = persisted as PlaygroundPrefs;
        // v0 saved presets whose values moved with the v1 palette; repoint
        // them so the swatch still shows as selected.
        if (version < 1 && s.accent in LEGACY_ACCENTS) {
          return { ...s, accent: LEGACY_ACCENTS[s.accent] };
        }
        return s;
      },
    },
  ),
);
