/**
 * Playground prefs, persisted per-device (localStorage) via zustand `persist` —
 * the same convention as theme.ts / ui.ts / active.ts — so the accent and an
 * acting name survive the "open full screen" tab and reloads.
 *
 * The `accent` is global (it's a look) and Playground-only: it repaints the
 * Playground subtree, never the rest of the app. The acting `name` is keyed by
 * project, because it becomes the caller identity (→ `HexgateContext.user_id`)
 * and must NOT silently ride from one project's agent to another.
 */

import type { CSSProperties } from "react";
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import { accentForeground, hexToHslChannels, hslChannelsToHex } from "./color";
import { SCHEMES, SCHEME_ACCENTS, schemeAccent, useIsDark } from "./theme";

/** The rose, amber and emerald presets (raw HSL, or the id this branch's v2
 * stored), dropped since red, amber and green mean deny, approval and
 * connected: a saved one resets to the app's. */
const DROPPED_ACCENTS = new Set([
  "340 75% 55%",
  "38 92% 50%",
  "152 60% 40%",
  "rose",
  "amber",
  "emerald",
]);

interface PlaygroundPrefs {
  /** "" = same as the app, a preset id, or a custom "#rrggbb". */
  accent: string;
  /** Acting name per project id (the demo caller identity). */
  names: Record<string, string>;
  /** Applied roles per project id, for the agent they were picked for; unset
   * = that agent's first role, [] = none. Kept here with the name, so a trip
   * to Audit or a full-screen tab keeps the role a call ran as. */
  roles: Record<string, { agent: string; roles: string[] }>;
  setAccent: (accent: string) => void;
  setName: (projectId: string, name: string) => void;
  setRoles: (projectId: string, agent: string, roles: string[]) => void;
}

/** The accent's `--primary` channels in this palette, or null for the app's. */
export function resolveAccent(accent: string, dark: boolean): string | null {
  if (!accent) return null;
  if (accent.startsWith("#")) return hexToHslChannels(accent);
  const preset = SCHEMES.find((p) => p.id === accent);
  return preset ? schemeAccent(preset.id, dark) : null;
}

/** CSS vars that repaint a subtree in `channels`: the highlight, focus ring
 * and main buttons (`--action`), each with a readable foreground. */
export function accentVars(channels: string | null): CSSProperties | undefined {
  if (!channels) return undefined;
  const fg = accentForeground(channels);
  return {
    "--primary": channels,
    "--ring": channels,
    "--primary-foreground": fg,
    "--action": channels,
    "--action-foreground": fg,
  } as CSSProperties;
}

/** CSS vars for the Playground accent in the current palette; spread onto the
 * Playground root and anything it portals (dialogs), so the accent stays
 * inside the Playground. */
export function usePlaygroundAccent() {
  const accent = usePlaygroundPrefs((s) => s.accent);
  const dark = useIsDark();
  return accentVars(resolveAccent(accent, dark));
}

/** v0 preset value → its v1 equivalent (old dark blue; old fixed violet). */
const V0_ACCENTS: Record<string, string> = {
  "226 78% 65%": SCHEME_ACCENTS.blue.dark,
  "262 70% 58%": SCHEME_ACCENTS.violet.dark,
};

/** A stored accent before v3 → a preset id: raw HSL when it matches a preset
 * in either palette, else the same colour as a hex; "" for a dropped preset.
 * An id or a hex (stored by v2) is kept. */
function accentToId(channels: string): string {
  if (!channels || DROPPED_ACCENTS.has(channels)) return "";
  if (channels.startsWith("#") || SCHEMES.some((p) => p.id === channels))
    return channels;
  const v = V0_ACCENTS[channels] ?? channels;
  const preset = SCHEMES.find((p) => p.light === v || p.dark === v);
  return preset ? preset.id : hslChannelsToHex(v);
}

export const usePlaygroundPrefs = create<PlaygroundPrefs>()(
  persist(
    (set) => ({
      accent: "",
      names: {},
      roles: {},
      setAccent: (accent) => set({ accent }),
      setName: (projectId, name) =>
        set((s) => ({ names: { ...s.names, [projectId]: name } })),
      setRoles: (projectId, agent, roles) =>
        set((s) => ({ roles: { ...s.roles, [projectId]: { agent, roles } } })),
    }),
    {
      name: "hexgate-playground",
      storage: createJSONStorage(() => localStorage),
      version: 3,
      migrate: (persisted, version) => {
        const s = persisted as PlaygroundPrefs;
        // Earlier versions stored raw HSL, pinning one palette's value; v3
        // stores a preset id (or a hex) so it follows light and dark.
        if (version < 3) return { ...s, accent: accentToId(s.accent ?? "") };
        return s;
      },
    },
  ),
);
