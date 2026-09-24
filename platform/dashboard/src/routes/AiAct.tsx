import { useState } from "react";
import {
  useIsMutating,
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  CircleAlert,
  ClipboardList,
  Download,
  FileText,
  LoaderCircle,
  Scale,
  TriangleAlert,
} from "lucide-react";
import { toast } from "sonner";

import { ClassificationDialog } from "@/components/ai-act/ClassificationDialog";
import { NoProjectEmptyState } from "@/components/NoProjectEmptyState";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useProjectScoped } from "@/lib/active";
import {
  RETENTION_DAYS,
  missingFieldLabels,
  periodEnd,
  periodStart,
  retentionPeriod,
  riskTierLabel,
  saveBlob,
  toDateInput,
} from "@/lib/ai-act";
import {
  AI_ACT_HISTORY_LIMIT,
  ApiError,
  api,
  type AgentClassificationRead,
  type AiActReport,
} from "@/lib/api";

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

/** Period bounds render in UTC, because that is the zone the period picker is
 * labelled in and the zone the annex states the period in. Rendering them
 * locally put a Paris operator's history row a day out from both the dates
 * they typed and the document they downloaded. */
function formatUtcDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { timeZone: "UTC" });
}

/** First 12 hex characters of the annex digest — enough to eyeball against a
 * downloaded annex, short enough for a table cell. The full digest travels
 * in the annex and the report's signature block. */
function shortDigest(sha256: string): string {
  return `${sha256.slice(0, 12)}…`;
}

/** Shared failure notice. A fetch that failed must never render as an empty
 * result: "no entry recorded" and "no report generated" are facts this page
 * exists to state, and it may only state them when it knows them. */
function LoadError({ what }: { what: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
      <TriangleAlert className="size-10 text-deny/60" />
      <div className="text-sm font-medium">Couldn't load {what}</div>
      <div className="max-w-xs text-xs text-muted-foreground">
        This is a load failure, not an empty result. Refresh to retry.
      </div>
    </div>
  );
}

/**
 * Region B — one row per registered agent, with the operator's classification
 * entry and what is still missing from it.
 *
 * Agents with no entry are never hidden: an incomplete inventory is itself
 * evidence, and the report lists it as a gap. The table waits for every entry
 * before it paints, because a not-yet-loaded entry is indistinguishable from
 * an unrecorded one, and "incomplete" is a claim this page should only make
 * once it is true.
 */
function InventoryPanel({ projectId }: { projectId: string }) {
  const [editing, setEditing] = useState<string | null>(null);

  const agentsQuery = useQuery({
    queryKey: ["agents", projectId],
    queryFn: () => api.listAgents(projectId),
  });
  const agents = agentsQuery.data ?? [];

  const classificationQueries = useQueries({
    queries: agents.map((a) => ({
      queryKey: ["ai-act", "classification", projectId, a.name],
      queryFn: () => api.getAgentClassification(a.name, projectId),
    })),
  });

  // `useQueries` returns results in the order it was handed them, and both
  // arrays are derived from `agents` within this render, so the index joins.
  const entries = agents.map((a, i) => {
    const q = classificationQueries[i];
    return {
      name: a.name,
      // Only a query with nothing to show counts as failed. A refetch that
      // blips after the entry has loaded leaves `data` in place, and the row
      // should keep reporting what it knows rather than flip to "Unavailable"
      // — the same guard the agent list below uses.
      failed: !!q?.isError && q?.data === undefined,
      entry: (q?.data ?? null) as AgentClassificationRead | null,
    };
  });

  const loading =
    agentsQuery.isLoading || classificationQueries.some((q) => q.isPending);
  // One rule for the whole panel: a refetch that blips while the last-good
  // list is still cached is not a failure to report, and the header counts
  // must not vanish from above a table that is still rendering rows.
  const agentsFailed = agentsQuery.isError && agentsQuery.data === undefined;
  const loaded = entries.filter((e) => !e.failed);
  const completeCount = loaded.filter((e) => e.entry?.complete).length;
  const unavailable = entries.length - loaded.length;
  const editingEntry = entries.find((e) => e.name === editing);

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex items-center gap-2 border-b border-border px-5 py-3.5">
        <ClipboardList className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">AI system inventory</span>
        {!loading && !agentsFailed && (
          <span className="text-sm text-muted-foreground">
            · {agents.length} registered · {completeCount} complete ·{" "}
            {loaded.length - completeCount} incomplete
            {unavailable > 0 && ` · ${unavailable} unavailable`}
          </span>
        )}
      </div>

      {agentsFailed ? (
        <LoadError what="the agent list" />
      ) : loading ? (
        <div className="p-12 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      ) : agents.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
          <ClipboardList className="size-12 text-muted-foreground/40" />
          <div className="text-sm font-medium">No agents registered</div>
          <div className="max-w-xs text-xs text-muted-foreground">
            Register an agent from the SDK to start recording classification
            entries for it.
          </div>
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted-foreground">
              <th className="px-5 py-2.5 text-left font-medium">Agent</th>
              <th className="px-5 py-2.5 text-left font-medium">Entry</th>
              <th className="px-5 py-2.5 text-left font-medium">Risk tier</th>
              <th className="px-5 py-2.5 text-left font-medium">Annex III</th>
              <th className="px-5 py-2.5 text-left font-medium">Oversight</th>
              <th className="px-5 py-2.5 text-left font-medium">Recorded</th>
              <th className="w-28 px-5 py-2.5" />
            </tr>
          </thead>
          <tbody>
            {entries.map(({ name, entry, failed }) => {
              const missing = missingFieldLabels(entry);
              return (
                <tr
                  key={name}
                  className="border-b border-border/50 last:border-0 hover:bg-accent/40"
                >
                  <td className="px-5 py-3 font-mono text-xs">{name}</td>
                  <td className="px-5 py-3">
                    {failed ? (
                      // Neither complete nor incomplete — we don't know which,
                      // so the row says so instead of guessing.
                      <Badge variant="outline" className="text-deny">
                        Unavailable
                      </Badge>
                    ) : entry?.complete ? (
                      <Badge variant="allow">Complete</Badge>
                    ) : (
                      <div className="flex flex-col gap-1">
                        <Badge variant="outline">Incomplete</Badge>
                        {missing.length > 0 && (
                          <span className="text-[11px] text-muted-foreground">
                            Missing: {missing.join(", ")}
                          </span>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="px-5 py-3 text-muted-foreground">
                    {riskTierLabel(entry?.risk_tier ?? null) ?? "—"}
                  </td>
                  <td className="px-5 py-3 text-muted-foreground">
                    {entry?.annex_iii_point || "—"}
                  </td>
                  <td className="px-5 py-3 text-muted-foreground">
                    {entry?.oversight_owner_name || "—"}
                  </td>
                  <td className="px-5 py-3 text-[13px] text-muted-foreground">
                    {entry?.recorded_at
                      ? formatDateTime(entry.recorded_at)
                      : "—"}
                  </td>
                  <td className="px-5 py-3 text-right">
                    {/* No edit affordance on a row whose entry failed to load:
                     * the blank form it would open with overwrites a recorded
                     * entry and its provenance on save. */}
                    {!failed && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-xs"
                        onClick={() => setEditing(name)}
                      >
                        {entry?.recorded ? "Edit entry" : "Record entry"}
                      </Button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {editingEntry && (
        <ClassificationDialog
          open
          onOpenChange={(o) => !o && setEditing(null)}
          projectId={projectId}
          agentName={editingEntry.name}
          classification={editingEntry.entry}
        />
      )}
    </div>
  );
}

/**
 * Turn a generation failure into copy the operator can act on.
 *
 * 400 is the endpoint's own refusal of the period — "entirely older than the
 * 180-day retention window", "start must be before end" — and says more than
 * anything this page could infer, so it is shown verbatim. 503 is ClickHouse
 * being unreachable, which is a retry rather than a mistake.
 */
function generateErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 400) return err.message;
    if (err.status === 503) {
      return "The audit log is temporarily unavailable. Try again in a moment.";
    }
    if (err.status === 403) {
      return "You don't have permission to generate reports in this project.";
    }
  }
  return "Could not generate the report.";
}

/** Region C — pick the period and generate. Defaults to the full retention
 * window, which is the widest period the audit tables can evidence. */
/** Mutation key for one project's generation. Keyed rather than kept in
 * component state so the in-flight indicator reads the mutation cache: a
 * generation takes seconds, and an operator who clicks over to Audit and back
 * would otherwise return to an idle-looking button and sign a second report
 * over the same period. Project-scoped so the banner never appears on a
 * project nothing was generated for — and so that switching project detaches
 * the observer (`MutationObserver.setOptions` resets on a key change) while
 * the running mutation keeps the options it was fired with. */
function generateKey(projectId: string) {
  return ["ai-act", "generate", projectId] as const;
}

function useIsGenerating(projectId: string): boolean {
  return useIsMutating({ mutationKey: generateKey(projectId) }) > 0;
}

function GeneratePanel({ projectId }: { projectId: string }) {
  const [from, setFrom] = useState(() => toDateInput(retentionPeriod().from));
  const [to, setTo] = useState(() => toDateInput(retentionPeriod().to));
  const qc = useQueryClient();
  const generating = useIsGenerating(projectId);

  const generate = useMutation({
    mutationKey: generateKey(projectId),
    mutationFn: () =>
      api.generateAiActReport(
        { from: periodStart(from), to: periodEnd(to) },
        projectId,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-act", "reports", projectId] });
      toast.success("Report generated");
    },
    onError: (err) => toast.error(generateErrorMessage(err)),
  });

  const invalidPeriod = !from || !to || from > to;

  return (
    <div className="rounded-lg border border-border bg-card p-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-wrap items-end gap-4">
          <div className="space-y-1.5">
            <Label htmlFor="period-from">From (UTC)</Label>
            <Input
              id="period-from"
              type="date"
              className="w-44"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="period-to">To (UTC)</Label>
            <Input
              id="period-to"
              type="date"
              className="w-44"
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
          </div>
          <p className="pb-2 text-xs text-muted-foreground">
            Defaults to the last {RETENTION_DAYS} days — records are kept that
            long, so an earlier period has nothing left to report.
          </p>
        </div>
        <Button
          className="gap-2"
          disabled={invalidPeriod || generating}
          onClick={() => generate.mutate()}
        >
          <FileText className="size-4" />
          {generating ? "Generating…" : "Generate report"}
        </Button>
      </div>
      {invalidPeriod && (
        <p className="mt-3 text-xs text-destructive">
          Pick a period that starts on or before it ends.
        </p>
      )}
    </div>
  );
}

/** Region D — every report generated for this project, with both downloads.
 * The annex is what the signature covers; the PDF renders it. */
function HistoryPanel({ projectId }: { projectId: string }) {
  // Assembling a 180-day window is seconds of synchronous work server-side,
  // so the panel says so rather than looking unchanged until the row appears.
  const generating = useIsGenerating(projectId);
  const reportsQuery = useQuery({
    queryKey: ["ai-act", "reports", projectId],
    queryFn: () => api.listAiActReports(projectId),
  });
  const reports = reportsQuery.data ?? [];

  async function download(report: AiActReport, kind: "annex" | "pdf") {
    try {
      const blob =
        kind === "annex"
          ? await api.downloadAiActAnnex(report.id, projectId)
          : await api.downloadAiActReportPdf(report.id, projectId);
      saveBlob(
        blob,
        // The annex endpoint sets this same name in Content-Disposition; the
        // PDF route names itself after the report id.
        kind === "annex" ? report.annex_filename : `${report.id}.pdf`,
      );
    } catch (err) {
      // Both routes 404 only on "report not found", which here means the row
      // on screen is stale rather than that the download is broken: say which
      // it is, because the two ask for different things from the operator.
      if (err instanceof ApiError && err.status === 404) {
        toast.error("That report no longer exists. Refresh the history.");
        return;
      }
      // The render is a separate step from the signing, so a failed render
      // (502) leaves the stored annex intact and is worth retrying. Point at
      // the annex, which is the artifact the signature actually covers.
      if (kind === "pdf" && err instanceof ApiError && err.status === 502) {
        toast.error("Could not render the PDF. The annex is still available.");
        return;
      }
      toast.error(
        kind === "annex"
          ? "Could not download the annex."
          : "Could not download the PDF.",
      );
    }
  }

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex items-center gap-2 border-b border-border px-5 py-3.5">
        <FileText className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">Generated reports</span>
        {reportsQuery.data !== undefined && (
          // A full page means the endpoint may be holding more: it answers
          // with a bare array and no total, so the honest label is "the
          // newest N", not a count of everything that exists.
          <span className="text-sm text-muted-foreground">
            {reports.length === AI_ACT_HISTORY_LIMIT
              ? `· newest ${reports.length}`
              : `· ${reports.length}`}
          </span>
        )}
      </div>

      {generating && (
        <div className="flex items-center gap-2 border-b border-border bg-muted/40 px-5 py-2.5 text-xs text-muted-foreground">
          <LoaderCircle className="size-3.5 animate-spin" />
          Assembling and signing the report over the selected period…
        </div>
      )}

      {reportsQuery.isError && reportsQuery.data === undefined ? (
        <LoadError what="the report history" />
      ) : reportsQuery.isLoading ? (
        <div className="p-12 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      ) : reports.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
          <FileText className="size-12 text-muted-foreground/40" />
          <div className="text-sm font-medium">No reports yet</div>
          <div className="max-w-xs text-xs text-muted-foreground">
            Generate one to capture the controls in place and the events
            recorded over a period.
          </div>
        </div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-[10px] uppercase tracking-wider text-muted-foreground">
              <th className="px-5 py-2.5 text-left font-medium">
                Period (UTC)
              </th>
              <th className="px-5 py-2.5 text-left font-medium">Generated</th>
              <th className="px-5 py-2.5 text-left font-medium">By</th>
              <th className="px-5 py-2.5 text-left font-medium">
                {/* "SHA-256", not "Digest": the column is only useful to
                      someone checking a downloaded annex against it, and that
                      reader knows the algorithm's name, not our word for it. */}
                SHA-256
              </th>
              <th className="w-56 px-5 py-2.5" />
            </tr>
          </thead>
          <tbody>
            {reports.map((r) => (
              <tr
                key={r.id}
                className="border-b border-border/50 last:border-0 hover:bg-accent/40"
              >
                <td className="px-5 py-3">
                  {formatUtcDate(r.period_start)} →{" "}
                  {formatUtcDate(r.period_end)}
                </td>
                <td className="px-5 py-3 text-[13px] text-muted-foreground">
                  {formatDateTime(r.generated_at)}
                </td>
                <td
                  className="px-5 py-3 text-[13px] text-muted-foreground"
                  title={r.generated_by_user_id ?? undefined}
                >
                  {/* Both null once the account is erased — the attribution
                      survives inside the signed annex, not in this view. */}
                  {r.generated_by_email ?? r.generated_by_user_id ?? "—"}
                </td>
                <td
                  className="px-5 py-3 font-mono text-xs text-muted-foreground"
                  title={r.annex_sha256}
                >
                  {shortDigest(r.annex_sha256)}
                </td>
                <td className="px-5 py-3 text-right">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-1.5 text-xs"
                    onClick={() => download(r, "pdf")}
                  >
                    <Download className="size-3.5" />
                    PDF
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-1.5 text-xs"
                    onClick={() => download(r, "annex")}
                  >
                    <Download className="size-3.5" />
                    Annex
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/**
 * AI Act page. One button — Generate report — producing a signed document per
 * project that evidences the controls Hexgate enforces on the project's
 * agents and the events recorded about them over a period.
 *
 * Copy rule for this whole route: controls in place and events recorded.
 * Nothing here says a system or its operator is compliant, and nothing here
 * classifies a system — section 1 of the report reproduces the operator's own
 * assertion, with the provenance Hexgate recorded alongside it.
 */
export function AiActPage() {
  const scope = useProjectScoped();

  if (scope.status === "no-project") {
    return (
      <div className="mx-auto max-w-[1400px]">
        <h1 className="text-2xl font-semibold tracking-tight">
          Evidence &amp; Reports
        </h1>
        <NoProjectEmptyState resource="AI Act evidence" />
      </div>
    );
  }

  const projectId = scope.projectId;

  return (
    <div className="mx-auto max-w-[1400px] space-y-5">
      <div>
        <h1 className="flex items-center gap-2.5 text-2xl font-semibold tracking-tight">
          <Scale className="size-6 text-primary" />
          Evidence &amp; Reports
        </h1>
        <p className="mt-2 max-w-[720px] text-sm text-muted-foreground">
          Generate a signed document describing the controls in place on this
          project's AI systems and the events recorded about them over a period.
        </p>
      </div>

      <div className="flex items-start gap-3 rounded-lg border border-border bg-muted/40 px-4 py-3 text-xs text-muted-foreground">
        <CircleAlert className="mt-px size-4 shrink-0" />
        <span>
          The report is Hexgate's own artifact, built from Hexgate's records. It
          is not a document defined by Regulation (EU) 2024/1689 and not a
          statutory filing, and it does not assert that any system, or its
          operator, is in conformity with that Regulation. The classification
          entries below are yours: Hexgate records who entered them and when,
          and never determines them for you.
        </span>
      </div>

      {!projectId ? (
        <div className="p-12 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      ) : (
        <>
          <InventoryPanel projectId={projectId} />
          <GeneratePanel projectId={projectId} />
          <HistoryPanel projectId={projectId} />
        </>
      )}
    </div>
  );
}
