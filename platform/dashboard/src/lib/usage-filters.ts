/**
 * Usage-page filter state.
 *
 * Zustand so the dialled-in slice survives navigating away and back within
 * the session — deliberately NOT ``persist``-ed: a stale filter from last
 * week silently narrowing today's usage view would read as missing data.
 * (Audit keeps its filters in the URL instead; see audit-filters.) No ``tableLimit``/``loadMore`` here — Usage has no events table.
 */

import { create } from "zustand";

import { fromAuditSearch, toAuditSearch } from "./audit-filters";
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

// A link into /usage with filters (e.g. from the agent hub) carries them in
// the query; the page loads them into the store once and drops the query.
const TEXT_KEYS = ["agent", "model", "user"] as const;

/** `/usage` with `f` in the query: the range keys /audit uses, plus text keys. */
export function usageUrl(f: Partial<UsageFilters>): string {
  const p = toAuditSearch({
    range: f.range ?? EMPTY_USAGE_FILTERS.range,
    customMode: f.customMode,
    start_date: f.start_date,
    end_date: f.end_date,
  });
  for (const k of TEXT_KEYS) if (f[k]) p.set(k, f[k]!);
  return `/usage?${p.toString()}`;
}

/** Filters from a `usageUrl` query, or null when it carries none. */
export function fromUsageSearch(p: URLSearchParams): UsageFilters | null {
  const range = fromAuditSearch(p);
  if (!range && !TEXT_KEYS.some((k) => p.has(k))) return null;
  const f: UsageFilters = { ...EMPTY_USAGE_FILTERS };
  if (range) {
    f.range = range.range;
    f.customMode = range.customMode;
    f.start_date = range.start_date;
    f.end_date = range.end_date;
  }
  for (const k of TEXT_KEYS) f[k] = p.get(k) ?? "";
  return f;
}
