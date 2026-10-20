import type { TokenListItem } from "@/lib/api";

export const STALE_KEY_DAYS = 30;

/** Unused for 30+ days. A never-used key counts from its creation, so one
 * minted a minute ago is not stale. */
export function isStaleKey(t: TokenListItem, now: number): boolean {
  return (
    now - +new Date(t.last_used_at ?? t.created_at) > STALE_KEY_DAYS * 864e5
  );
}
