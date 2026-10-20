/**
 * Audit-page filters and paging.
 *
 * The URL is the source of truth for the filters (see routes/Audit.tsx):
 * links, reloads and new tabs open the same slice. This store only keeps
 * the last view's query string, so a bare /audit (the sidebar) reopens it,
 * and how many rows that view has loaded. Not persisted: a filter from last
 * week silently narrowing today's view would read as missing data.
 */

import { endOfDay, format, isValid, parse, startOfDay } from "date-fns";
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
  /** Query string of the last Audit view; a bare /audit reopens it. */
  lastSearch: string;
  remember: (search: string) => void;
  /** Rows loaded for the view `pageKey` — any other view starts at one page. */
  pageKey: string;
  pageLimit: number;
  loadMore: (key: string) => void;
}

export const useAuditFilters = create<AuditFilterState>()((set) => ({
  lastSearch: "",
  remember: (search) => set({ lastSearch: search }),
  pageKey: "",
  pageLimit: PAGE_SIZE,
  loadMore: (key) =>
    set((s) => ({
      pageKey: key,
      pageLimit: Math.min(
        (s.pageKey === key ? s.pageLimit : PAGE_SIZE) + PAGE_SIZE,
        MAX_LIMIT,
      ),
    })),
}));

/** Rows to show for view `key`. */
export function pageLimitFor(
  s: Pick<AuditFilterState, "pageKey" | "pageLimit">,
  key: string,
): number {
  return s.pageKey === key ? s.pageLimit : PAGE_SIZE;
}

// ——— URL form ————————————————————————————————————————————————————————
// Audit's filters live in its URL (see routes/Audit.tsx), so a link, a
// reload or a new tab opens the same slice. Custom dates travel as local
// calendar days, so a shared link shows the same days in any timezone.

const TEXT_KEYS = ["agent", "role", "tool", "user"] as const;
const URL_KEYS = ["range", ...TEXT_KEYS, "outcome", "from", "to", "custom"];
const OUTCOMES: readonly AuditOutcome[] = ["allow", "deny", "needs_approval"];

/** Search params for `f`: range always, other fields only when set. */
export function toAuditSearch(f: Partial<AuditFilters>): URLSearchParams {
  const p = new URLSearchParams();
  p.set("range", f.range ?? EMPTY_AUDIT_FILTERS.range);
  for (const k of TEXT_KEYS) if (f[k]) p.set(k, f[k]!);
  if (f.outcome) p.set("outcome", f.outcome);
  if (f.customMode) p.set("custom", "1");
  if (f.start_date) p.set("from", format(f.start_date, "yyyy-MM-dd"));
  if (f.end_date) p.set("to", format(f.end_date, "yyyy-MM-dd"));
  return p;
}

const parseDay = (v: string | null): Date | null => {
  if (!v) return null;
  const d = parse(v, "yyyy-MM-dd", new Date());
  return isValid(d) ? d : null;
};

/** Filters from search params, or null when the URL carries none (a bare
 * /audit). Unknown or malformed values fall back to defaults. */
export function fromAuditSearch(p: URLSearchParams): AuditFilters | null {
  if (!URL_KEYS.some((k) => p.has(k))) return null;
  const f: AuditFilters = { ...EMPTY_AUDIT_FILTERS };
  const range = p.get("range");
  if (range && Object.hasOwn(RANGE_DAYS, range)) f.range = range as Range;
  for (const k of TEXT_KEYS) f[k] = p.get(k) ?? "";
  const outcome = p.get("outcome");
  if (outcome && (OUTCOMES as readonly string[]).includes(outcome))
    f.outcome = outcome as AuditOutcome;
  const from = parseDay(p.get("from"));
  const to = parseDay(p.get("to"));
  if (from) f.start_date = startOfDay(from);
  if (to) f.end_date = endOfDay(to);
  // Any date makes it a custom range, so a date filter is never hidden.
  f.customMode = p.get("custom") === "1" || !!from || !!to;
  return f;
}

/** `prev` with its Audit filter keys replaced by `f`'s; any other param
 * (e.g. `present`) is kept. */
export function withAuditFilters(
  prev: URLSearchParams,
  f: Partial<AuditFilters>,
): URLSearchParams {
  const next = new URLSearchParams(prev);
  for (const k of URL_KEYS) next.delete(k);
  toAuditSearch(f).forEach((v, k) => next.set(k, v));
  return next;
}

/** A link into Audit showing exactly `f` (everything else at defaults). */
export function auditUrl(f: Partial<AuditFilters> = {}): string {
  return `/audit?${toAuditSearch(f).toString()}`;
}
