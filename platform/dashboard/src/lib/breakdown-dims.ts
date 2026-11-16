/** The dimensions a breakdown card offers: all of them, or the caller's
 * subset in the caller's order (the first is selected). */
export function pickDims<D extends { id: string }>(
  all: readonly D[],
  ids?: readonly [D["id"], ...D["id"][]],
): D[] {
  if (!ids) return [...all];
  return ids.map((id) => all.find((d) => d.id === id)!);
}
