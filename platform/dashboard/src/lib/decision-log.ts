import { useCallback, useMemo, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import {
  api,
  type AuditDecisionFilters,
  type AuditDecisionPage,
  type AuditDecisionRow,
} from "@/lib/api";
import { saveBlob } from "@/lib/ai-act";
import { nextPageLimit } from "@/lib/audit-filters";

/** A decision log page with its drawer: the list (polled, paged by the
 * caller's limit), the picked row (dropped on a project switch), its
 * same-session siblings, and an export of the loaded rows. Shared by the
 * Audit page and the agent hub. */
export function useDecisionLog(
  projectId: string | null,
  filters: AuditDecisionFilters & { limit: number },
  setLimit: (limit: number) => void,
  exportName: string,
) {
  // The pick belongs to the project it was made in.
  const [picked, setPicked] = useState<{
    project: string | null;
    row: AuditDecisionRow;
  } | null>(null);
  if (picked && picked.project !== projectId) setPicked(null);
  const sel = picked?.row ?? null;
  const setSel = useCallback(
    (row: AuditDecisionRow | null) =>
      setPicked(row && { project: projectId, row }),
    [projectId],
  );
  const clearSel = useCallback(() => setPicked(null), []);

  const listQ = useQuery({
    queryKey: ["audit", "list", projectId, filters],
    enabled: !!projectId,
    queryFn: () => api.listAuditDecisions(filters, projectId as string),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
  // keepPreviousData covers a bigger page only while it loads: if it fails,
  // keep the last page of the same filters rather than wipe the table.
  const base = JSON.stringify([projectId, { ...filters, limit: undefined }]);
  const [kept, setKept] = useState<{
    base: string;
    page: AuditDecisionPage;
    limit: number;
  } | null>(null);
  if (
    listQ.data &&
    !listQ.isPlaceholderData &&
    (kept?.base !== base || kept.page !== listQ.data)
  )
    setKept({ base, page: listQ.data, limit: filters.limit });
  const ours = kept?.base === base ? kept : null;
  const page = listQ.data ?? ours?.page;

  // One page past the one shown, so a failed page is asked for again, not
  // skipped.
  const loadMore = () => {
    const shown =
      listQ.data && !listQ.isPlaceholderData
        ? filters.limit
        : (ours?.limit ?? filters.limit);
    const next = nextPageLimit(shown);
    if (next === filters.limit) void listQ.refetch();
    else setLimit(next);
  };
  // A bigger page (or a retry of a failed one) in flight; not the 30s poll.
  const loadingMore =
    listQ.isFetching && (listQ.isPlaceholderData || listQ.isError);

  const relatedQ = useQuery({
    queryKey: ["audit", "session", projectId, filters.window, sel?.session_id],
    enabled: !!sel?.session_id && !!projectId,
    queryFn: () =>
      api.listAuditDecisions(
        { window: filters.window, session_id: sel!.session_id, limit: 12 },
        projectId as string,
      ),
  });
  const related = useMemo(
    () =>
      (relatedQ.data?.rows ?? [])
        .filter((r) => r.event_id !== sel?.event_id)
        .slice(0, 6),
    [relatedQ.data, sel],
  );

  // The loaded page only (at most MAX_LIMIT rows), not every match: that
  // would need a streaming endpoint.
  const exportJsonl = () => {
    const rows = page?.rows ?? [];
    if (!rows.length) return;
    saveBlob(
      new Blob([rows.map((r) => JSON.stringify(r)).join("\n")], {
        type: "application/x-ndjson",
      }),
      exportName,
    );
  };

  return {
    listQ,
    page,
    loadMore,
    loadingMore,
    sel,
    setSel,
    clearSel,
    related,
    exportJsonl,
  };
}
