/** "1 tool", "3 tools". */
export function pluralise(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

/** Compact token counts (1.25M / 84.6K) — raw counts run into the millions
 * and read poorly uncompacted in a stat tile. Shared by Usage and Home. */
export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

/** "3m ago" for a cell; pass `now` so a ticking clock re-renders it. */
export function formatRelative(iso: string, now = Date.now()): string {
  const diff = now - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return `${Math.floor(diff / 86_400_000)}d ago`;
}

/** The full local date and time, for a tooltip beside a relative time. */
export function formatAbsolute(iso: string): string {
  return new Date(iso).toLocaleString();
}
