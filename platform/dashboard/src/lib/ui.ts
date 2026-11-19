/**
 * Small persisted UI-preference store (separate from `active`, which holds the
 * org/project scope): the workspace-sidebar collapsed state, so a collapse
 * survives a reload, and the quickstart dialog's open/step/seen state.
 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

interface UiState {
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;
  /** Whether the quickstart dialog is showing. Not persisted: a reload never
   * reopens it on its own. */
  quickstartOpen: boolean;
  /** The quickstart step on screen (0-based). Kept across a close, so leaving
   * for a page from a step and reopening resumes there. Not persisted. */
  quickstartStep: number;
  /** Set once the user has closed the quickstart, so Home only offers it
   * unprompted the first time. Persisted. */
  quickstartSeen: boolean;
  /** Open the quickstart, at `step` when given (else where it was left). */
  openQuickstart: (step?: number) => void;
  setQuickstartStep: (step: number) => void;
  /** Close it; `reset` sends the next open back to the first step. */
  closeQuickstart: (opts?: { reset?: boolean }) => void;
}

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      toggleSidebar: () =>
        set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      quickstartOpen: false,
      quickstartStep: 0,
      quickstartSeen: false,
      openQuickstart: (step) =>
        set((s) => ({
          quickstartOpen: true,
          quickstartStep: step ?? s.quickstartStep,
        })),
      setQuickstartStep: (step) => set({ quickstartStep: step }),
      closeQuickstart: (opts) =>
        set((s) => ({
          quickstartOpen: false,
          quickstartSeen: true,
          quickstartStep: opts?.reset ? 0 : s.quickstartStep,
        })),
    }),
    {
      name: "hexgate-ui",
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({
        sidebarCollapsed: s.sidebarCollapsed,
        quickstartSeen: s.quickstartSeen,
      }),
    },
  ),
);
