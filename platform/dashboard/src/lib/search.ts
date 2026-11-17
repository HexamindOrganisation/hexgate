/** The pickers' and filters' name search: case-insensitive "contains". */

/** A search box's text as the matcher reads it. */
export const normalizeQuery = (query: string) => query.trim().toLowerCase();

/** Whether `text` matches a normalised query ("" matches everything). */
export const matchesQuery = (text: string, q: string) =>
  !q || text.toLowerCase().includes(q);
