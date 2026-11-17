/** A dashboard range (preset or custom dates) kept in the URL, under the same
 * keys /audit uses (`range`, `from`, `to`, `custom`), so a reload or a shared
 * link keeps it. Only those keys are read or written; any other param is left
 * alone. Used by the agent hub's Stats and Usage tabs. */

import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import {
  EMPTY_AUDIT_FILTERS,
  fromAuditSearch,
  toAuditSearch,
} from "./audit-filters";
import type { Range, RangePickerFilters } from "./date-range";

export const RANGE_KEYS = ["range", "from", "to", "custom"] as const;

/** Just the range keys of `params`, for carrying a range across links. */
export function rangeSearch(params: URLSearchParams): string {
  const p = new URLSearchParams();
  for (const k of RANGE_KEYS) {
    const v = params.get(k);
    if (v !== null) p.set(k, v);
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

function parseRange(params: URLSearchParams, fallback: Range) {
  const { range, customMode, start_date, end_date } = fromAuditSearch(
    new URLSearchParams(rangeSearch(params)),
  ) ?? { ...EMPTY_AUDIT_FILTERS, range: fallback };
  return { range, customMode, start_date, end_date };
}

/** The API scope for a range: the window plus ISO custom dates. */
export function rangeScope(f: RangePickerFilters) {
  return {
    window: f.range,
    start_date: f.start_date ? f.start_date.toISOString() : undefined,
    end_date: f.end_date ? f.end_date.toISOString() : undefined,
  };
}

export function useRangeParams(
  defaultRange: Range,
): [
  RangePickerFilters,
  (update: (prev: RangePickerFilters) => RangePickerFilters) => void,
] {
  const [params, setParams] = useSearchParams();
  const f = useMemo(
    () => parseRange(params, defaultRange),
    [params, defaultRange],
  );
  const setF = (update: (prev: RangePickerFilters) => RangePickerFilters) =>
    setParams(
      (prev) => {
        // Apply to the latest URL, not the render's `f`, so chained updates
        // compose.
        const next = new URLSearchParams(prev);
        for (const k of RANGE_KEYS) next.delete(k);
        toAuditSearch(update(parseRange(prev, defaultRange))).forEach((v, k) =>
          next.set(k, v),
        );
        return next;
      },
      { replace: true },
    );
  return [f, setF];
}
