/**
 * Usage-page filter state.
 *
 * Zustand so the dialled-in slice survives navigating away and back within
 * the session — deliberately NOT ``persist``-ed: a stale filter from last
 * week silently narrowing today's usage view would read as missing data.
 * (Audit keeps its filters in the URL instead; see audit-filters.) No ``tableLimit``/``loadMore`` here — Usage has no events table.
 */

import { create } from "zustand";

import type { BaseDashboardFilters } from "./date-range";

// '' = "all".
export interface UsageFilters extends BaseDashboardFilters {
  model: string;
}

export type SetUsageFilters = (
  updater: (prev: UsageFilters) => UsageFilters,
) => void;

export const EMPTY_USAGE_FILTERS: UsageFilters = {
  agent: "",
  model: "",
  user: "",
  range: "30d",
  customMode: false,
  start_date: null,
  end_date: null,
};

interface UsageFilterState {
  filters: UsageFilters;
  setFilters: SetUsageFilters;
}

export const useUsageFilters = create<UsageFilterState>()((set) => ({
  filters: EMPTY_USAGE_FILTERS,
  setFilters: (updater) => set((s) => ({ filters: updater(s.filters) })),
}));
