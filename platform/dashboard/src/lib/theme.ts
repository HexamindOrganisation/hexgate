/**
 * App theme: two independent axes.
 *   - `mode`   — light / dark / system (system follows prefers-color-scheme).
 *   - `scheme` — accent: blue (default), violet, plum, teal or graphite.
 *
 * Persisted per-device (localStorage) and applied to <html> as the `.dark`
 * class + a `data-scheme` attribute, which the token blocks in index.css key
 * off. `useApplyTheme` (mounted once at the app root) keeps <html> in sync and
 * follows the OS when mode is "system".
 */

import { useEffect, useState } from "react";
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type Mode = "light" | "dark" | "system";
export type Scheme = "blue" | "violet" | "plum" | "teal" | "graphite";

/**
 * The built-in accent schemes as `--primary` HSL channels, per palette —
 * the single source of truth mirroring the token blocks in index.css. Both the
 * appearance-menu swatches and the Playground's accent presets read
 * from here, so they agree with the real CSS token in light AND dark (a preset
 * that hardcoded one mode's value wouldn't match the built-in in the other).
 */
export const SCHEME_ACCENTS: Record<Scheme, { light: string; dark: string }> = {
  blue: { light: "226 70% 52%", dark: "225 81% 69%" },
  violet: { light: "254 71% 59%", dark: "252 85% 74%" },
  plum: { light: "313 62% 48%", dark: "313 75% 68%" },
  teal: { light: "180 81% 29%", dark: "179 52% 50%" },
  graphite: { light: "218 16% 45%", dark: "217 18% 76%" },
};

/** Display order and labels for the accent picker. */
export const SCHEMES: { id: Scheme; label: string }[] = [
  { id: "blue", label: "Blue" },
  { id: "violet", label: "Violet" },
  { id: "plum", label: "Plum" },
  { id: "teal", label: "Teal" },
  { id: "graphite", label: "Graphite" },
];

/** The `--primary` channels for a scheme in the currently-resolved palette. */
export function schemeAccent(scheme: Scheme, dark: boolean): string {
  const a = SCHEME_ACCENTS[scheme];
  return dark ? a.dark : a.light;
}

interface ThemeState {
  mode: Mode;
  scheme: Scheme;
  setMode: (mode: Mode) => void;
  setScheme: (scheme: Scheme) => void;
}

export const useTheme = create<ThemeState>()(
  persist(
    (set) => ({
      // Default to dark/blue — the app's established look — so existing users
      // see no change until they opt into light or another accent.
      mode: "dark",
      scheme: "blue",
      setMode: (mode) => set({ mode }),
      setScheme: (scheme) => set({ scheme }),
    }),
    { name: "hexgate-theme", storage: createJSONStorage(() => localStorage) },
  ),
);

const prefersDark = () =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-color-scheme: dark)").matches;

/** Whether the given mode resolves to a dark palette right now. */
export function isDark(mode: Mode): boolean {
  return mode === "dark" || (mode === "system" && prefersDark());
}

/** Write the resolved theme onto <html>. Safe to call before React mounts. */
export function applyTheme(mode: Mode, scheme: Scheme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", isDark(mode));
  root.dataset.scheme = scheme;
}

/**
 * Reactive "is the palette dark right now" — tracks the store mode and, while
 * mode is "system", the OS preference. For consumers that must re-render on a
 * mode flip (e.g. the CodeMirror theme), not just re-style via CSS.
 */
export function useIsDark(): boolean {
  const mode = useTheme((s) => s.mode);
  const [systemDark, setSystemDark] = useState(prefersDark);
  useEffect(() => {
    if (mode !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => setSystemDark(mq.matches);
    setSystemDark(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [mode]);
  return mode === "dark" || (mode === "system" && systemDark);
}

/**
 * Keep <html> in sync with the store, and re-apply on OS scheme changes while
 * mode is "system". Mount once near the app root.
 */
export function useApplyTheme(): void {
  const mode = useTheme((s) => s.mode);
  const scheme = useTheme((s) => s.scheme);

  useEffect(() => {
    applyTheme(mode, scheme);
    if (mode !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme(mode, scheme);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [mode, scheme]);
}
