/** Compact token counts (1.25M / 84.6K) — raw counts run into the millions
 * and read poorly uncompacted in a stat tile. Shared by Usage and Home. */
export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}
