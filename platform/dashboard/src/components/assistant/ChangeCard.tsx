import { memo, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleMinus,
  FileDiff,
  XCircle,
} from "lucide-react";

import { FilesDiff } from "@/components/assistant/FilesDiff";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { ChangeCheck, ChangeCheckId } from "@/lib/api";
import type { AssistantChange } from "@/lib/assistant";
import { usePolicyChange } from "@/lib/policy_files";
import { cn } from "@/lib/utils";

/** What each platform check proves, in the admin's words. */
const CHECK_LABEL: Record<ChangeCheckId, string> = {
  yaml: "Every file parses",
  composes: "The policy composes for every agent",
  lints: "No new lint error",
  dry_runs: "The sample calls get the expected decision",
  boundary: "No boundary loosened",
  bundle: "The enforcement bundle is rebuilt",
};

const STATUS_ICON = {
  pass: { Icon: CheckCircle2, tone: "text-allow", label: "passed" },
  warn: { Icon: AlertTriangle, tone: "text-approval", label: "warning" },
  fail: { Icon: XCircle, tone: "text-deny", label: "failed" },
  skipped: {
    Icon: CircleMinus,
    tone: "text-muted-foreground",
    label: "skipped",
  },
} as const;

/**
 * A change the assistant saved: its summary, the checks it passed, and its
 * files, each one's diff a click away. The stream carries names and checks
 * only; the files' text is read from the stored change.
 */
export const ChangeCard = memo(function ChangeCard({
  projectId,
  change,
}: {
  projectId: string;
  change: AssistantChange;
}) {
  return (
    <section
      aria-label="Saved change"
      className="rounded-lg border border-border bg-muted/20 text-xs"
    >
      <header className="border-b border-border/60 px-3 py-2">
        <p className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          Change saved
        </p>
        <p className="mt-0.5">{change.summary}</p>
      </header>
      <ul className="space-y-1 px-3 py-2">
        {change.checks.map((c) => (
          <CheckRow key={c.id} check={c} />
        ))}
      </ul>
      <ChangeFiles projectId={projectId} change={change} />
    </section>
  );
});

function CheckRow({ check }: { check: ChangeCheck }) {
  // The wire may carry a status or id newer than this build knows.
  const status = STATUS_ICON[check.status] ?? STATUS_ICON.skipped;
  const { Icon } = status;
  return (
    <li className="flex items-start gap-1.5">
      <Icon
        aria-label={status.label}
        className={cn("mt-px size-3.5 shrink-0", status.tone)}
      />
      <div className="min-w-0">
        <p>{CHECK_LABEL[check.id] ?? check.id}</p>
        {check.detail && (
          <p className="break-words text-muted-foreground">{check.detail}</p>
        )}
      </div>
    </li>
  );
}

/** The change's files, one picked at a time, and the picked one's diff. */
function ChangeFiles({
  projectId,
  change,
}: {
  projectId: string;
  change: AssistantChange;
}) {
  const [picked, setPicked] = useState(change.files[0].name);
  const stored = usePolicyChange(projectId, change.change_id);
  const file = stored.data?.files.find((f) => f.name === picked);
  return (
    <div className="border-t border-border/60">
      <Tabs value={picked} onValueChange={setPicked}>
        <TabsList
          aria-label="Changed files"
          className="flex h-auto flex-wrap justify-start gap-1 border-0 bg-transparent px-3 py-2"
        >
          {change.files.map((f) => (
            <TabsTrigger
              key={f.name}
              value={f.name}
              title={f.name}
              className="max-w-full gap-1 rounded border border-transparent px-1.5 py-0.5 font-mono text-[11px] font-normal hover:text-foreground data-[state=active]:border-border data-[state=active]:bg-transparent"
            >
              <FileDiff className="size-3 shrink-0" />
              <span className="truncate">{f.name}</span>
              {f.before_hash === null && (
                <span className="text-allow">new</span>
              )}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value={picked} className="mt-0 border-t border-border/60">
          {file ? (
            <FilesDiff
              before={file.before_content}
              after={file.after_content}
            />
          ) : (
            <p className="px-3 py-2 text-muted-foreground">
              {stored.isError
                ? "The change's files could not be loaded."
                : "Loading the diff…"}
            </p>
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
