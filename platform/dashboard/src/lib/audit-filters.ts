/**
 * Audit-page filter + paging state.
 *
 * Zustand (like ``useActive``) rather than page-local ``useState`` so the
 * slice the user dialled in survives navigating away and back within the
 * session — deliberately NOT ``persist``-ed: a stale filter from last week
 * silently narrowing today's audit view would read as missing data.
 *
 * Folding ``tableLimit`` into the same store lets ``setFilters`` reset
 * paging atomically — any filter change starts the table back at page one,
 * without the page component wiring that invariant by hand.
 */

import { create } from "zustand";

import type { AuditOutcome } from "./api";
import {
  type BaseDashboardFilters,
  RANGE_DAYS,
  type Range,
} from "./date-range";

export { RANGE_DAYS };

// '' = "all"; outcome applies to the events table only.
export interface AuditFilters extends BaseDashboardFilters {
  role: string;
  tool: string;
  outcome: "" | AuditOutcome;
}

export type SetAuditFilters = (
  updater: (prev: AuditFilters) => AuditFilters,
) => void;

export const EMPTY_AUDIT_FILTERS: AuditFilters = {
  agent: "",
  role: "",
  tool: "",
  user: "",
  outcome: "",
  range: "30d",
  customMode: false,
  start_date: null,
  end_date: null,
};

const PAGE_SIZE = 40;
const MAX_LIMIT = 200;

interface AuditFilterState {
  filters: AuditFilters;
  tableLimit: number;
  setFilters: SetAuditFilters;
  loadMore: () => void;
}

export const useAuditFilters = create<AuditFilterState>()((set) => ({
  filters: EMPTY_AUDIT_FILTERS,
  tableLimit: PAGE_SIZE,
  setFilters: (updater) =>
    set((s) => ({ filters: updater(s.filters), tableLimit: PAGE_SIZE })),
  loadMore: () =>
    set((s) => ({ tableLimit: Math.min(s.tableLimit + PAGE_SIZE, MAX_LIMIT) })),
}));

// ——— URL form ————————————————————————————————————————————————————————
// Audit's filters live in its URL too, so a link, a reload or a new tab
// opens the same slice. `range` is always written: a URL that carries it is
// authoritative and replaces the session's filters; a bare /audit (the
// sidebar) keeps them.

const TEXT_KEYS = ["agent", "role", "tool", "user"] as const;
const RANGES: readonly Range[] = ["24h", "7d", "30d", "90d"];
const OUTCOMES: readonly AuditOutcome[] = ["allow", "deny", "needs_approval"];

/** Search params for `f`: range always, other fields only when set. */
export function toAuditSearch(f: Partial<AuditFilters>): URLSearchParams {
  const p = new URLSearchParams();
  p.set("range", f.range ?? EMPTY_AUDIT_FILTERS.range);
  for (const k of TEXT_KEYS) if (f[k]) p.set(k, f[k]!);
  if (f.outcome) p.set("outcome", f.outcome);
  if (f.start_date && f.end_date) {
    p.set("from", f.start_date.toISOString());
    p.set("to", f.end_date.toISOString());
  }
  return p;
}

/** Full filters from search params, or null when the URL doesn't carry
 * them (no `range`). Unknown or malformed values fall back to defaults. */
export function fromAuditSearch(p: URLSearchParams): AuditFilters | null {
  const range = p.get("range");
  if (!range) return null;
  const f: AuditFilters = { ...EMPTY_AUDIT_FILTERS };
  if ((RANGES as readonly string[]).includes(range)) f.range = range as Range;
  for (const k of TEXT_KEYS) f[k] = p.get(k) ?? "";
  const outcome = p.get("outcome");
  if (outcome && (OUTCOMES as readonly string[]).includes(outcome))
    f.outcome = outcome as AuditOutcome;
  const from = new Date(p.get("from") ?? "");
  const to = new Date(p.get("to") ?? "");
  if (!isNaN(+from) && !isNaN(+to)) {
    f.customMode = true;
    f.start_date = from;
    f.end_date = to;
  }
  return f;
}

/** A link into Audit showing exactly `f` (everything else at defaults). */
export function auditUrl(f: Partial<AuditFilters> = {}): string {
  return `/audit?${toAuditSearch(f).toString()}`;
}
