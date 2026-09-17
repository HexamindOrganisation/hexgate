import { useState } from "react";
import {
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
  isClassificationComplete,
  missingClassificationFields,
  periodEnd,
  periodStart,
  retentionPeriod,
  riskTierLabel,
  saveBlob,
  toDateInput,
} from "@/lib/ai-act";
import {
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
      queryFn: () =>
        api.getAgentClassification(a.name, projectId).catch((err) => {
          // The endpoint answers 200 with a prefilled body for an agent that
          // has no entry. Tolerate a 404 as the same answer, so whichever of
          // those two shapes PR 1 lands with reads as "nothing recorded"
          // rather than as a failed load.
          if (err instanceof ApiError && err.status === 404) return null;
          throw err;
        }),
    })),
  });

  // `useQueries` returns results in the order it was handed them, and both
  // arrays are derived from `agents` within this render, so the index joins.
  const entries = agents.map((a, i) => {
    const q = classificationQueries[i];
    return {
      name: a.name,
      failed: !!q?.isError,
      entry: (q?.data ?? null) as AgentClassificationRead | null,
    };
  });

  const loading =
    agentsQuery.isLoading || classificationQueries.some((q) => q.isPending);
  const loaded = entries.filter((e) => !e.failed);
  const completeCount = loaded.filter((e) =>
    isClassificationComplete(e.entry),
  ).length;
  const unavailable = entries.length - loaded.length;
  const editingEntry = entries.find((e) => e.name === editing);

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="flex items-center gap-2 border-b border-border px-5 py-3.5">
        <ClipboardList className="size-4 text-muted-foreground" />
        <span className="text-sm font-medium">AI system inventory</span>
        {!loading && !agentsQuery.isError && (
          <span className="text-sm text-muted-foreground">
            · {agents.length} registered · {completeCount} complete ·{" "}
            {loaded.length - completeCount} incomplete
            {unavailable > 0 && ` · ${unavailable} unavailable`}
          </span>
        )}
      </div>

      {agentsQuery.isError && agentsQuery.data === undefined ? (
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
              const missing = missingClassificationFields(entry);
              const complete = isClassificationComplete(entry);
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
                    ) : complete ? (
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
                        {entry?.recorded_at ? "Edit entry" : "Record entry"}
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

/** Region C — pick the period and generate. Defaults to the full retention
 * window, which is the widest period the audit tables can evidence. */
function GeneratePanel({ projectId }: { projectId: string }) {
  const [from, setFrom] = useState(() => toDateInput(retentionPeriod().from));
  const [to, setTo] = useState(() => toDateInput(retentionPeriod().to));
  const qc = useQueryClient();

  const generate = useMutation({
    mutationFn: () =>
      api.generateAiActReport(
        { from: periodStart(from), to: periodEnd(to) },
        projectId,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-act", "reports", projectId] });
      toast.success("Report generated");
    },
    onError: () => toast.error("Could not generate the report."),
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
          disabled={invalidPeriod || generate.isPending}
          onClick={() => generate.mutate()}
        >
          <FileText className="size-4" />
          {generate.isPending ? "Generating…" : "Generate report"}
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
        kind === "annex" ? `${report.id}-annex.json` : `${report.id}.pdf`,
      );
    } catch {
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
          <span className="text-sm text-muted-foreground">
            · {reports.length}
          </span>
        )}
      </div>

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
              <th className="px-5 py-2.5 text-left font-medium">Digest</th>
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
                  title={r.generated_by_user_id}
                >
                  {r.generated_by_email ?? r.generated_by_user_id}
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
        <h1 className="text-2xl font-semibold tracking-tight">AI Act</h1>
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
          AI Act
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
